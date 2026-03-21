---
title: "DevSecOps Cheatsheet"
description: "Quick reference for security scanning tools and commands"
sidebar_label: "Cheatsheet"
sidebar_position: 3
---

# DevSecOps Cheatsheet

Quick reference for security scanning tools, commands, and configurations.

## Secrets Detection

### GitLeaks

```bash
# Install
brew install gitleaks

# Scan staged files (pre-commit)
gitleaks detect --staged

# Scan entire repository history
gitleaks detect --verbose

# Scan specific directory
gitleaks detect --source={path}

# Scan with custom rules
gitleaks detect --rules-path=custom-rules.toml

# Generate report
gitleaks detect --report-path=report.json
```

### Pre-commit Hook

```bash
# .git/hooks/pre-commit
#!/bin/bash
gitleaks detect --staged
if [ $? -ne 0 ]; then
  echo "Secrets detected. Commit aborted."
  exit 1
fi
```

### TruffleHog

```bash
# Install
pip install truffleHog

# Scan repository
trufflehog git file:///path/to/repo

# Scan with high entropy check
trufflehog git file:///path/to/repo --only-verified

# Scan for specific secret type
trufflehog git file:///path/to/repo --custom-regexes=patterns.json
```

## Static Application Security Testing (SAST)

### Semgrep

```bash
# Install
pip install semgrep

# Scan current directory
semgrep --config=p/security-audit .

# Scan with multiple rulesets
semgrep --config=p/security-audit --config=p/owasp-top-ten .

# Output as SARIF
semgrep --config=p/security-audit --format=sarif --output=results.sarif .

# Run specific rule
semgrep --config=p/python-security --include=*.py .

# Interactive mode
semgrep --interactive
```

### SonarQube

```bash
# Scan with SonarScanner CLI
sonar-scanner \
  -Dsonar.projectKey=myapp \
  -Dsonar.sources=. \
  -Dsonar.host.url=https://sonarqube.example.com \
  -Dsonar.login=<token>

# Maven integration
mvn clean verify sonar:sonar \
  -Dsonar.projectKey=myapp \
  -Dsonar.host.url=https://sonarqube.example.com
```

### Bandit (Python)

```bash
# Install
pip install bandit

# Scan Python code
bandit -r /path/to/code

# Generate report
bandit -r . -f json -o report.json

# Exclude specific tests
bandit -r . -skip B101,B601

# Scan with custom configuration
bandit -r . -c bandit.yaml
```

## Software Composition Analysis (SCA)

### Snyk

```bash
# Install
npm install -g snyk

# Authenticate
snyk auth

# Test dependencies
snyk test

# Test with detailed output
snyk test --severity-threshold=high

# Fix vulnerabilities
snyk fix

# Monitor continuously
snyk monitor

# Scan specific file
snyk test requirements.txt

# Generate report
snyk test --json > report.json

# Scan Dockerfile
snyk container test myapp:latest
```

### Dependabot

```yaml
# .github/dependabot.yml
version: 2
updates:
  - package-ecosystem: "npm"
    directory: "/"
    schedule:
      interval: "weekly"
    open-pull-requests-limit: 10
    reviewers:
      - "security-team"

  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "daily"
    allow:
      - dependency-type: "direct"
```

### Safety (Python)

```bash
# Install
pip install safety

# Check for vulnerabilities
safety check

# Check specific requirements file
safety check -r requirements.txt

# JSON output
safety check --json

# Generate report
safety check --output report.txt
```

## Container Security Scanning

### Trivy

```bash
# Install
brew install aquasecurity/trivy/trivy

# Scan Docker image
trivy image myapp:latest

# Scan with severity filter
trivy image --severity HIGH,CRITICAL myapp:latest

# Generate JSON report
trivy image --format json --output report.json myapp:latest

# Scan Dockerfile
trivy config Dockerfile

# Scan filesystem
trivy fs /path/to/code

# Scan Git repository
trivy repo https://github.com/user/repo.git

# Scan image and ignore specific CVEs
trivy image --skip-db-update --ignorefile .trivyignore myapp:latest
```

### Grype

```bash
# Install
brew install grype

# Scan image
grype myapp:latest

# Scan with specific matcher
grype myapp:latest --fail-on critical

# Output as JSON
grype myapp:latest --output json

# Scan directory
grype dir:/path/to/code

# Generate SBOM
grype myapp:latest --output syft-json
```

### Clair

```bash
# Setup database
docker-compose up -d postgres
docker-compose up -d clair

# Scan image
curl -X POST http://localhost:6060/v1/layers \
  -H "Content-Type: application/json" \
  -d '{
    "Layer": {
      "Name": "myapp:latest",
      "Path": "/path/to/image.tar",
      "Format": "Docker"
    }
  }'
```

## Infrastructure-as-Code Scanning

### Checkov

```bash
# Install
pip install checkov

# Scan Terraform
checkov -d .

# Scan specific file
checkov -f main.tf

# Scan Kubernetes
checkov -f deployment.yaml --framework kubernetes

# Check specific check
checkov -d . --check CKV_AWS_1

# Skip specific checks
checkov -d . --skip-check CKV_AWS_1,CKV_AWS_2

# Output as JSON
checkov -d . --output json

# HTML report
checkov -d . --output cli --output-file-path report.html
```

### Snyk IaC

```bash
# Test Terraform
snyk iac test main.tf

# Test Kubernetes manifest
snyk iac test deployment.yaml

# JSON output
snyk iac test main.tf --json

# Severity threshold
snyk iac test main.tf --severity-threshold=high

# Custom policies
snyk iac test main.tf --policy-engine=custom
```

### Terraform Validate

```bash
# Validate syntax
terraform validate

# Check formatting
terraform fmt -check=true

# Custom rules (tflint)
tflint

# Run specific rule
tflint --filter=terraform_unused_required_providers
```

## Dynamic Application Security Testing (DAST)

### OWASP ZAP

```bash
# Baseline scan (fast)
docker run -t owasp/zap2docker-stable zap-baseline.py \
  -t https://example.com

# Full scan (slower, comprehensive)
docker run -t owasp/zap2docker-stable zap-full-scan.py \
  -t https://example.com \
  -J report.json

# API scan
docker run -t owasp/zap2docker-stable zap-api-scan.py \
  -t https://api.example.com/swagger.json

# Generate report in different formats
docker run -t owasp/zap2docker-stable zap-baseline.py \
  -t https://example.com \
  -J report.json \
  -H report.html
```

### Nuclei

```bash
# Install
go install -v github.com/projectdiscovery/nuclei/v2/cmd/nuclei@latest

# Scan with template
nuclei -l urls.txt -t cves/

# Scan specific template
nuclei -u https://example.com -t cves/2023/wordpress-plugin.yaml

# Custom template
nuclei -u https://example.com -t custom-template.yaml

# Output as JSON
nuclei -u https://example.com -t cves/ -j -o results.json

# Rate limiting
nuclei -u https://example.com -t cves/ -rl 50
```

## Security Headers

### HTTPS/TLS

```bash
# Check certificate
openssl s_client -connect example.com:443 -showcerts

# Verify TLS version
openssl s_client -connect example.com:443 -tls1_3

# Certificate expiry
echo | openssl s_client -servername example.com -connect example.com:443 \
  2>/dev/null | openssl x509 -noout -dates
```

### Security Headers

```
# Content-Security-Policy
Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'

# X-Content-Type-Options
X-Content-Type-Options: nosniff

# X-Frame-Options
X-Frame-Options: DENY

# X-XSS-Protection
X-XSS-Protection: 1; mode=block

# Strict-Transport-Security
Strict-Transport-Security: max-age=31536000; includeSubDomains

# Referrer-Policy
Referrer-Policy: strict-origin-when-cross-origin

# Permissions-Policy
Permissions-Policy: geolocation=(), microphone=(), camera=()
```

### Test Headers

```bash
# Check security headers
curl -I https://example.com | grep -i "security\|x-\|strict\|content-security"

# Full security header test
curl -s -D - https://example.com -o /dev/null | grep -E "^[A-Z]"

# Using online tools
https://securityheaders.com/?q=example.com
```

## Cryptography Reference

### TLS/SSL Versions

```
TLS 1.3   ✓ Use this (latest and most secure)
TLS 1.2   ✓ Use this (acceptable for legacy)
TLS 1.1   ✗ Avoid (deprecated)
TLS 1.0   ✗ Avoid (deprecated)
SSL 3.0   ✗ Avoid (deprecated)
```

### Cipher Suites

**Strong (use these):**
```
TLS_AES_256_GCM_SHA384           # TLS 1.3
TLS_AES_128_GCM_SHA256           # TLS 1.3
TLS_CHACHA20_POLY1305_SHA256     # TLS 1.3
ECDHE-ECDSA-AES256-GCM-SHA384    # TLS 1.2
ECDHE-RSA-AES256-GCM-SHA384      # TLS 1.2
```

**Weak (avoid these):**
```
RC4                      # Broken cipher
DES                      # Broken cipher
NULL                     # No encryption
MD5                      # Weak hash
SHA1                     # Weak hash
```

### Hashing Algorithms

```
SHA-256     ✓ Use this (NIST approved)
SHA-384     ✓ Use this (NIST approved)
SHA-512     ✓ Use this (NIST approved)
SHA-1       ✗ Avoid (deprecated)
MD5         ✗ Avoid (broken)
```

### Encryption Algorithms

```
AES-256-GCM      ✓ Use this (recommended)
AES-256-CBC      ✓ Use this (acceptable)
AES-128-GCM      ✓ Use this (acceptable)
3DES             ✗ Avoid (weak)
RC4              ✗ Avoid (broken)
```

## Common Vulnerabilities Quick Fix

### SQL Injection

```python
# Vulnerable
query = f"SELECT * FROM users WHERE id = {user_id}"
db.execute(query)

# Fixed
query = "SELECT * FROM users WHERE id = ?"
db.execute(query, (user_id,))
```

### Cross-Site Scripting (XSS)

```html
<!-- Vulnerable -->
<div><%= user_comment %></div>

<!-- Fixed -->
<div><%- user_comment %></div>
<!-- or -->
<div><%= sanitize(user_comment) %></div>
```

### Hardcoded Secrets

```python
# Vulnerable
DATABASE_PASSWORD = "secretpassword123"

# Fixed
import os
DATABASE_PASSWORD = os.environ.get("DATABASE_PASSWORD")
```

### Insecure Deserialization

```python
# Vulnerable
import pickle
data = pickle.loads(user_input)

# Fixed
import json
data = json.loads(user_input)
```

## Security Scanning in CI/CD

### GitHub Actions

```yaml
name: Security Scan
on: [push, pull_request]

jobs:
  security:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Secrets scanning
        run: |
          pip install gitleaks
          gitleaks detect --staged

      - name: SAST scan
        uses: returntocorp/semgrep-action@v1

      - name: Container scan
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: myapp:latest
```

### GitLab CI

```yaml
security:
  stage: test
  script:
    - gitleaks detect --staged
    - semgrep --config=p/security-audit .
    - snyk test
    - trivy image myapp:latest
```

## Key Takeaways

- **Automate security scanning** in CI/CD pipeline
- **Shift-left** — catch issues early
- **Use multiple tools** — different tools catch different issues
- **Track metrics** — measure and improve security
- **Educate developers** — make security accessible
- **Keep tools updated** — vulnerability databases change daily
- **Balance security and speed** — don't block deployments unnecessarily
