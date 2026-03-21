---
title: "Ansible"
description: "Configuration management and automation"
---

# Ansible

Agentless automation for configuration management, application deployment, and orchestration.

## Why Ansible?

- **Agentless** — Uses SSH, no agents to install
- **YAML-based** — Human-readable playbooks
- **Idempotent** — Safe to run multiple times
- **Extensible** — 3000+ modules available

## Key Concepts

| Concept | Description |
|:--------|:------------|
| **Inventory** | INI file with server information |
| **Playbook** | YAML file defining automation tasks |
| **Role** | Reusable collection of tasks, vars, templates |
| **Module** | Unit of work (e.g., `apt`, `copy`, `service`) |
| **Vault** | Encrypt sensitive data |
| **Control Node** | Machine where Ansible is installed |
| **Task** | Single procedure to execute (e.g., install package) |
| **Play** | Full provisioning execution from start to finish |
| **Handler** | Trigger service status changes |
| **Facts** | Global variables about the system |

## Example Playbook

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

    - name: Deploy configuration
      template:
        src: nginx.conf.j2
        dest: /etc/nginx/nginx.conf
      notify: restart nginx

  handlers:
    - name: restart nginx
      service:
        name: nginx
        state: restarted
```

## Essential Commands

```bash
# Run a playbook
ansible-playbook site.yml

# Check mode (dry run)
ansible-playbook site.yml --check

# Limit to specific hosts
ansible-playbook site.yml --limit webservers

# Use vault
ansible-vault encrypt secrets.yml
ansible-playbook site.yml --ask-vault-pass

# Ad-hoc commands
ansible all -m ping
ansible webservers -m shell -a "uptime"
```

## Learning Resources

### Official Documentation

| Resource | Description |
|:---------|:------------|
| [Ansible Documentation](https://docs.ansible.com/) | Official Ansible docs |
| [Ansible Modules](https://docs.ansible.com/ansible/latest/collections/) | Complete module reference |

### Tutorials and Guides

| Resource | Description |
|:---------|:------------|
| [Configuration Management 101](https://www.digitalocean.com/community/tutorials/configuration-management-101-writing-ansible-playbooks) | Writing Ansible Playbooks |
| [Why You Might Need Ansible](https://www.freecodecamp.org/news/why-you-might-need-ansible-and-not-even-know-it-d33b6e4b2ebe/) | Use cases and benefits |
| [Ansible Roles & Jenkins Integration](https://www.softwaretestinghelp.com/ansible-roles-jenkins-integration-ec2-modules/) | Ansible with Jenkins and EC2 |
| [Ansible with Docker](https://mklein.io/2018/02/23/ansible-docker/) | Using Ansible on Docker |

## Getting Started

### Basic Workflow

1. **Prepare inventory** — Define your managed nodes in INI format
2. **Connect** — Ansible connects via SSH (no agents needed)
3. **Copy modules** — Ansible copies modules to remote machines
4. **Execute** — Modules run on remote systems

### Working with Modules

- Ansible ships with a module library for system resources
- Control services, packages, files, and more
- Handle executing system commands
- Create custom modules for specific needs

## Features

- **Radically simple** — YAML-based playbooks anyone can read
- **Agentless** — No agents to install or manage
- **Cloud-ready** — Manage cloud instances immediately
- **Idempotent** — Safe to run multiple times
- **IT Orchestration** — Zero-downtime updates, hotfixes
- **Extensible** — 3000+ built-in modules

## Books & PDFs

| Book | Link |
|:-----|:-----|
| Ansible Automation | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Ansible/Books/Ansible%20Automation.pdf) |
| Ansible from Beginner to Pro | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Ansible/Books/Ansible%20From%20Beginner%20To%20Pro.pdf) |
| Ansible Full Course | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Ansible/Books/Ansible%20Full%20Course.pdf) |
| Ansible Playbook Essentials | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Ansible/Books/Ansible%20Playbook%20Essentials.pdf) |
| Ansible Playbook | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Ansible/Books/Ansible%20Playbook.pdf) |
| Ansible for AWS | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Ansible/Books/Ansible%20for%20AWS.pdf) |
| Expertise in Ansible Automation | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Ansible/Books/Expertise%20in%20Ansible%20Automation.pdf) |
| How to Manage Remote Servers with Ansible | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Ansible/Books/How%20to%20manage%20remote%20servers%20with%20Ansible.pdf) |
| Learn Ansible Quickly | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Ansible/Books/Learn%20Ansible%20Quickly.pdf) |
| Learning Ansible | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Ansible/Books/Learning%20Ansible.pdf) |
| Ansible Tutorial | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Ansible/Books/Ansible%20tutorial.pdf) |
