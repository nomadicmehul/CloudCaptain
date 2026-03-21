---
title: "GitHub Actions"
description: "CI/CD and automation platform built into GitHub"
---

# GitHub Actions

Automate workflows directly from your GitHub repository. Build, test, and deploy code automatically on every push or pull request.

## Core Topics

| Topic | Description |
|:------|:------------|
| [Fundamentals](fundamentals.md) | Workflows, events, jobs, steps, actions, runners, secrets, matrix builds |
| [Cheatsheet](cheatsheet.md) | Quick reference for YAML syntax, common actions, and expressions |

## Key Features

- **Integrated CI/CD** — Built directly into GitHub
- **Free for public repos** — Unlimited free CI/CD
- **Matrix builds** — Test across multiple environments
- **Secrets management** — Secure credential storage
- **Reusable workflows** — Share automation across repos
- **Community actions** — Leverage 10,000+ community actions
- **Self-hosted runners** — Run on your own infrastructure

## Key Concepts

- **Workflow**: YAML file that defines automated process
- **Event**: GitHub action that triggers workflow (push, PR, schedule)
- **Job**: Set of steps that run on same runner
- **Step**: Individual task (action or shell command)
- **Action**: Reusable code unit
- **Runner**: Machine that executes your jobs
- **Secret**: Securely stored sensitive data (API keys, passwords)

## Common Use Cases

- Run tests on every pull request
- Build and publish Docker images
- Deploy to cloud platforms (AWS, Azure, GCP)
- Release and publish packages to npm, PyPI
- Automated security scanning (SAST, dependency checks)
- Create/update issues and pull requests
- Notify teams on Slack, Discord

## Getting Started

1. Create `.github/workflows/` directory
2. Add a `.yml` workflow file
3. Define triggers (push, pull_request, schedule, etc.)
4. Add jobs with steps
5. Commit and push to trigger workflow

```yaml
name: CI

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-node@v3
        with:
          node-version: 16
      - run: npm test
```

## Popular Resources

| Resource | Topic |
|:---------|:------|
| [GitHub Actions Docs](https://docs.github.com/en/actions) | Official documentation |
| [Workflow Syntax](https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions) | Complete YAML reference |
| [GitHub Marketplace](https://github.com/marketplace?type=actions) | Browse 10,000+ actions |
| [Awesome Actions](https://github.com/sdras/awesome-actions) | Curated list of Actions |

## Contributing

Know great GitHub Actions resources? Submit a PR to help the community learn!
