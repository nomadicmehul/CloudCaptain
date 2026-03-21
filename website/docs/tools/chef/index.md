---
title: "Chef"
sidebar_label: "Chef Overview"
description: "Infrastructure automation with Chef Infra — cookbooks, recipes, roles, and configuration management"
sidebar_position: 0
---

# Chef

Chef is an infrastructure automation framework that uses code to define, manage, and scale infrastructure. It enables teams to automate provisioning, configuration, and management of systems across cloud, on-premises, and hybrid environments.

## What is Chef?

Chef automates infrastructure by allowing you to express configuration as code. Following a declarative model, you define desired state, and Chef ensures systems converge to that state through idempotent operations.

**Core principles:**
- **Infrastructure as Code** — Version-control your infrastructure
- **Idempotent** — Safe to run multiple times
- **Declarative** — Define "what", not "how"
- **Convergence** — Systems reach desired state automatically

## Key Components

- **Chef Infra Client** — Converges nodes to desired state
- **Chef Infra Server** — Central management hub for cookbooks and node data
- **Chef Workstation** — Development environment (Chef CLI, Knife, Test Kitchen, Cookstyle)
- **Cookbooks** — Reusable configuration packages
- **InSpec** — Infrastructure testing and compliance
- **Chef Automate** — Dashboards, visibility, and analytics

## Quick Start

### Install

```bash
# Install Chef Workstation
brew install chef-workstation

# Verify
chef --version
knife --version
```

### Create Cookbook

```bash
chef generate cookbook nginx_web
cd nginx_web
```

### Write Recipe

```ruby
# recipes/default.rb

package 'nginx' do
  action :install
end

service 'nginx' do
  action [:enable, :start]
end

template '/etc/nginx/nginx.conf' do
  source 'nginx.conf.erb'
  notifies :restart, 'service[nginx]'
end
```

### Test with Test Kitchen

```bash
kitchen test
```

## Documentation

| Guide | Description |
|:------|:------------|
| [Chef Fundamentals](/docs/tools/chef/fundamentals) | Cookbooks, recipes, resources, attributes, roles, environments, Chef Infra Server |
| [Chef Cheat Sheet](/docs/tools/chef/cheatsheet) | Knife commands, resource reference, role/environment patterns, Test Kitchen |

## Core Concepts

### Cookbooks

Collections of recipes, attributes, templates, and files that define infrastructure.

```
my_cookbook/
├── recipes/              # Recipe definitions
├── attributes/           # Default values
├── templates/            # Configuration templates
├── files/               # Static files
└── metadata.rb          # Cookbook metadata
```

### Recipes

Ruby code that defines resources and configuration.

```ruby
package 'nginx'
service 'nginx' { action :start }
template '/etc/nginx/nginx.conf' { source 'nginx.conf.erb' }
```

### Resources

Building blocks representing system components (packages, services, files, users, etc.).

### Roles

Grouping of recipes and attributes for specific purposes (webserver, database, etc.).

### Environments

Separation of infrastructure stages (dev, staging, production).

## Chef Resources

| Resource | Purpose |
|----------|---------|
| `package` | Install/remove packages |
| `service` | Manage system services |
| `file` | Create/manage files |
| `directory` | Create/manage directories |
| `template` | Manage files from templates |
| `user` | Manage user accounts |
| `group` | Manage user groups |
| `execute` | Run commands |
| `git` | Clone/manage git repos |
| `cron` | Manage cron jobs |

## Common Tasks

### Manage Nodes

```bash
knife node list                    # List all nodes
knife node show node-name          # Show node details
knife bootstrap 192.168.1.100 -x ubuntu  # Bootstrap new node
knife ssh 'role:webserver' 'sudo chef-client'  # Run chef on nodes
```

### Manage Cookbooks

```bash
knife cookbook upload my_cookbook  # Upload to server
knife cookbook list               # List on server
knife cookbook download my_cookbook  # Download from server
```

### Manage Roles

```bash
knife role create webserver        # Create role
knife role upload webserver        # Upload role
knife role list                    # List roles
```

### Test Cookbooks

```bash
kitchen create                     # Create test instances
kitchen converge                   # Run chef
kitchen verify                     # Run tests
kitchen test                       # Full cycle
```

## Why Chef?

1. **Scalability** — Manage thousands of nodes
2. **Flexibility** — Ruby-based DSL for complex requirements
3. **Idempotence** — Safe repeated runs
4. **Testing** — Test Kitchen and InSpec integration
5. **Ecosystem** — Supermarket has thousands of cookbooks
6. **Enterprise** — Chef Automate for visibility and compliance

## Use Cases

- **Cloud provisioning** — Automate instance setup
- **Configuration management** — Keep systems in desired state
- **Compliance** — Verify and enforce security policies
- **Application deployment** — Deploy and manage applications
- **Multi-environment** — Separate dev, staging, production

## Best Practices

1. **Use attributes** — Make recipes configurable
2. **Write idempotent recipes** — Safe to run repeatedly
3. **Test thoroughly** — Use Test Kitchen and InSpec
4. **Version cookbooks** — Track changes in git
5. **Use roles and environments** — Organize infrastructure
6. **Document recipes** — Include README and comments
7. **Follow conventions** — Use cookbook structure standards

## Chef Infra Server

Central hub for Chef infrastructure:
- Store and manage cookbooks
- Store node attributes and data
- Manage policies and run lists
- Role-based access control
- Secure node authentication

## Integration with Clouds

Chef works with:
- **AWS** — EC2, RDS, S3
- **Azure** — Virtual Machines, App Service
- **GCP** — Compute Engine
- **Kubernetes** — Container orchestration

## Next Steps

1. **[Chef Fundamentals](/docs/tools/chef/fundamentals)** — Learn cookbooks, recipes, resources
2. **[Chef Cheat Sheet](/docs/tools/chef/cheatsheet)** — Knife commands and patterns
3. **[Official Documentation](https://docs.chef.io/)** — Complete reference
4. **[Supermarket](https://supermarket.chef.io/)** — Community cookbooks
5. **[InSpec](https://docs.chef.io/inspec/)** — Infrastructure testing

## Common Commands

```bash
chef generate cookbook NAME       # Create cookbook
knife cookbook upload COOKBOOK    # Upload to server
knife node list                   # List nodes
kitchen test                      # Test cookbook
cookstyle COOKBOOK               # Check style
```

## Contributing

Have Chef tips, cookbooks, or best practices to share? [Contribute](https://github.com/nomadicmehul/CloudCaptain) to CloudCaptain!
