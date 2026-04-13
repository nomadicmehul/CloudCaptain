# Security Policy

## Supported Versions

CloudCaptain is a continuously-deployed static site. The only "supported version" is the content currently live at [cloudcaptain.io](https://cloudcaptain.io), which is built from the `main` branch.

## Reporting a Vulnerability

If you believe you've found a security issue, **please do not open a public GitHub issue**. Instead:

1. Email the maintainer privately at **security@cloudcaptain.io** (or via the contact form at [nomadicmehul.dev](https://nomadicmehul.dev/)).
2. Alternatively, use GitHub's [private vulnerability reporting](https://github.com/nomadicmehul/CloudCaptain/security/advisories/new) feature.

Please include:

- A clear description of the issue
- Steps to reproduce
- Affected URL(s) or files
- Any proof-of-concept payloads (if applicable)

We'll acknowledge your report within **72 hours** and aim to publish a fix or mitigation within **7 days** for verified issues.

## Scope

In-scope:

- The `cloudcaptain.io` website and its source in this repository
- GitHub Actions workflows in `.github/workflows/`
- Any first-party scripts or components under `website/src/`

Out-of-scope:

- Third-party sites linked from CloudCaptain content
- Social engineering of maintainers
- Denial-of-service against GitHub Pages infrastructure
- Automated vulnerability scanner output without a working proof-of-concept

## Thanks

Responsible disclosure keeps the community safe. Contributors who report valid issues will be credited (with permission) in release notes.
