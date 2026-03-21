---
title: "YAML"
sidebar_label: "YAML Overview"
description: "YAML syntax, configuration patterns, and DevOps usage"
sidebar_position: 0
---

# YAML

YAML (YAML Ain't Markup Language) is the configuration language of DevOps — essential for Kubernetes, Ansible, Docker Compose, GitHub Actions, AWS CodeBuild, and countless other infrastructure tools.

## What is YAML?

YAML is a human-readable data serialization language designed for configuration files and data exchange. As a superset of JSON with minimal syntax overhead, YAML enables clean, readable infrastructure-as-code across the entire DevOps ecosystem.

**Why YAML matters:**
- Human-readable syntax reduces errors and improves maintainability
- Used by virtually every major DevOps and cloud-native tool
- Supports complex data structures while remaining lightweight
- Minimal learning curve compared to XML or JSON

## Documentation

| Guide | Description |
|:------|:------------|
| [YAML Fundamentals](/docs/tools/yaml/fundamentals) | Syntax, data types, sequences, mappings, anchors/aliases, multi-document, and JSON/XML comparison |
| [YAML Cheat Sheet](/docs/tools/yaml/cheatsheet) | Quick reference for syntax patterns and DevOps configurations (Kubernetes, Ansible, Docker Compose, GitHub Actions) |

## Key Concepts

- **Indentation-sensitive** — Whitespace defines structure
- **Case-sensitive** — Keys and values differentiate uppercase and lowercase
- **Superset of JSON** — All valid JSON is valid YAML
- **Rich type system** — Strings, numbers, booleans, nulls, dates, collections
- **Anchors & Aliases** — DRY principle through reusable definitions
- **Comments** — Inline and block comments with `#`

## Quick Examples

### Basic Key-Value
```yaml
name: John Doe
age: 30
city: New York
```

### Lists and Mappings
```yaml
skills:
  - Docker
  - Kubernetes
  - Terraform

database:
  host: localhost
  port: 5432
  username: admin
```

### Kubernetes Pod
```yaml
apiVersion: v1
kind: Pod
metadata:
  name: my-app
spec:
  containers:
  - name: app
    image: myapp:1.0
    ports:
    - containerPort: 8080
```

### Ansible Playbook
```yaml
---
- name: Deploy application
  hosts: webservers
  tasks:
    - name: Install dependencies
      apt:
        name: docker.io
        state: present
    - name: Start service
      systemd:
        name: docker
        state: started
```

## Common DevOps Use Cases

- **Kubernetes** — Pod definitions, deployments, services, ConfigMaps
- **Ansible** — Playbooks, inventory, variable definitions
- **Docker Compose** — Multi-container application orchestration
- **GitHub Actions** — CI/CD workflow automation
- **Terraform** — Input variables and outputs
- **AWS CodeBuild** — Build specifications and pipelines

## Getting Started

1. **Learn the fundamentals** — Start with basic syntax and data types
2. **Practice with examples** — Work through real Kubernetes and Ansible configs
3. **Use a validator** — Check your YAML with online tools or `yamllint`
4. **Master DevOps patterns** — Study Kubernetes manifests, Ansible playbooks
5. **Reference the cheat sheet** — Keep syntax patterns at your fingertips

## Tools

- **Online Validators** — [YAML Lint](http://www.yamllint.com/), [OnlineYAML](https://www.onlineyaml.com/)
- **CLI Tools** — `yamllint`, `yq`, `jq`
- **IDE Plugins** — VS Code, Vim, Emacs all have excellent YAML support

## Common Gotchas

- **Unquoted `no`/`yes`** — Interpreted as booleans, not strings
- **Tabs vs spaces** — YAML requires spaces; tabs cause silent failures
- **Missing quotes on numbers** — `"123"` vs `123`
- **Implicit nulls** — Empty values become `null`

## Contributing

Have YAML tips, patterns, or best practices to share? [Contribute](https://github.com/nomadicmehul/CloudCaptain) to CloudCaptain!

## Next Steps

- **[YAML Fundamentals](/docs/tools/yaml/fundamentals)** — Deep dive into syntax and data types
- **[YAML Cheat Sheet](/docs/tools/yaml/cheatsheet)** — Quick reference for DevOps patterns
- **[Kubernetes Documentation](https://kubernetes.io/docs/)** — Real-world YAML at scale
- **[Ansible Documentation](https://docs.ansible.com/)** — Infrastructure automation with YAML
