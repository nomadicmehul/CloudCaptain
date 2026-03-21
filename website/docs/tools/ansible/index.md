---
title: "Ansible"
sidebar_label: "Ansible Overview"
description: "Comprehensive Ansible learning resources — playbooks, roles, modules, automation patterns, and interview prep"
sidebar_position: 0
---

# Ansible

Agentless automation for configuration management, application deployment, and orchestration.

## Documentation

| Guide | Description |
|:------|:------------|
| [Ansible Fundamentals](/docs/tools/ansible/fundamentals) | Inventory, playbooks, roles, modules, Vault, Jinja2, Galaxy, exercises |
| [Playbook Examples](/docs/tools/ansible/examples) | Real-world playbooks: nginx, Docker, KVM, Windows, Vault, Vagrant |
| [Command Cheat Sheet](/docs/tools/ansible/cheatsheet) | 80+ commands and modules — ad-hoc, playbook, vault, galaxy, debugging |
| [Interview Questions](/docs/tools/ansible/interview-questions) | 38 questions from beginner to advanced with detailed answers |

## Why Ansible?

- **Agentless** — Uses SSH, no agents to install
- **YAML-based** — Human-readable playbooks
- **Idempotent** — Safe to run multiple times
- **Extensible** — 3000+ modules available

## Learning Path

1. [Start with fundamentals](/docs/tools/ansible/fundamentals) — inventory, playbooks, roles, modules, Vault
2. [Keep the cheat sheet handy](/docs/tools/ansible/cheatsheet) — 80+ commands and modules
3. [Practice interview questions](/docs/tools/ansible/interview-questions) — 38 Q&A with detailed explanations

## Key Concepts

| Concept | Description |
|:--------|:------------|
| **Inventory** | INI/YAML file with server information |
| **Playbook** | YAML file defining automation tasks |
| **Role** | Reusable collection of tasks, vars, templates |
| **Module** | Unit of work (e.g., `apt`, `copy`, `service`) |
| **Vault** | Encrypt sensitive data |
| **Handler** | Trigger service status changes |
| **Facts** | Global variables about the system |

## Quick Start

```yaml
---
- name: Configure web servers
  hosts: webservers
  become: yes
  tasks:
    - name: Install nginx
      apt:
        name: nginx
        state: present

    - name: Start nginx
      service:
        name: nginx
        state: started
        enabled: yes
```

```bash
# Run a playbook
ansible-playbook site.yml

# Check mode (dry run)
ansible-playbook site.yml --check

# Ad-hoc commands
ansible all -m ping
ansible webservers -m shell -a "uptime"

# Vault
ansible-vault encrypt secrets.yml
```

## External Resources

| Resource | Description |
|:---------|:------------|
| [Ansible Documentation](https://docs.ansible.com/) | Official Ansible docs |
| [Ansible Modules Index](https://docs.ansible.com/ansible/latest/collections/) | Complete module reference |
| [Configuration Management 101](https://www.digitalocean.com/community/tutorials/configuration-management-101-writing-ansible-playbooks) | Writing Ansible Playbooks |
| [Ansible with Docker](https://mklein.io/2018/02/23/ansible-docker/) | Using Ansible on Docker |
| [Ansible Roles & Jenkins](https://www.softwaretestinghelp.com/ansible-roles-jenkins-integration-ec2-modules/) | Integration with Jenkins and EC2 |
