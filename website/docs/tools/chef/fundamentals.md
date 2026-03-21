---
title: "Chef Fundamentals"
sidebar_label: "Fundamentals"
description: "Chef architecture, cookbooks, recipes, resources, attributes, roles, environments, and Infra Server"
sidebar_position: 1
---

# Chef Fundamentals

Chef is an infrastructure automation framework that uses code to define and manage your infrastructure. It enables teams to automate the provisioning, configuration, and management of systems at scale.

## What is Chef?

Chef automates infrastructure management by allowing you to express configuration as code. It follows a declarative model: you define desired state, and Chef ensures systems converge to that state.

**Core philosophy:**
- **Infrastructure as Code** — Version control your infrastructure
- **Idempotent** — Safe to run multiple times
- **Declarative** — Define "what" not "how"
- **Convergence** — Systems reach desired state through repeated runs

## Chef Components

### The Chef Ecosystem

```
┌─────────────────────────────────────────────────┐
│ Chef Workstation                                │
│ - Chef CLI, Knife, Test Kitchen, Cookstyle     │
└──────────────────────┬──────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        │              │              │
        ▼              ▼              ▼
   ┌─────────┐  ┌─────────┐  ┌──────────────┐
   │Cookbooks│  │InSpec   │  │Chef Automate │
   │Recipes  │  │Tests    │  │(dashboards)  │
   └────┬────┘  └─────────┘  └──────────────┘
        │
        ▼
┌─────────────────────────────────────────────────┐
│ Chef Infra Server                               │
│ - Cookbooks, Policies, Node data                │
└──────────────────────┬──────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        │              │              │
        ▼              ▼              ▼
   ┌─────────┐  ┌─────────┐  ┌─────────┐
   │ Node A  │  │ Node B  │  │ Node C  │
   │Chef-    │  │Chef-    │  │Chef-    │
   │client   │  │client   │  │client   │
   └─────────┘  └─────────┘  └─────────┘
```

## Chef Concepts

### Cookbooks

A cookbook is a collection of recipes, attributes, templates, and files that define infrastructure. It's the basic unit of reusable code in Chef.

```
my_cookbook/
├── recipes/                # Recipe definitions
│   ├── default.rb
│   ├── install.rb
│   └── configure.rb
├── attributes/             # Default values
│   ├── default.rb
│   └── packages.rb
├── templates/              # File templates
│   └── app.conf.erb
├── files/                  # Static files
│   └── config.yaml
├── metadata.rb             # Cookbook metadata
├── README.md
└── CHANGELOG.md
```

### Recipes

Recipes are Ruby code that defines resources and actions.

```ruby
# recipes/default.rb

# Update package manager
execute 'update-packages' do
  command 'apt-get update'
  action :run
end

# Install package
package 'nginx' do
  action :install
end

# Create directory
directory '/var/www/html' do
  mode '0755'
  recursive true
end

# Create file from template
template '/etc/nginx/sites-available/default' do
  source 'nginx.conf.erb'
  variables(
    server_name: node['nginx']['server_name']
  )
  notifies :restart, 'service[nginx]'
end

# Manage service
service 'nginx' do
  action [:enable, :start]
end
```

### Resources

Resources are the building blocks of recipes. Each resource represents a system component.

```ruby
# Core resources
package 'nginx'                    # Install package
service 'nginx'                    # Manage service
file '/etc/config.conf'            # Manage file
directory '/var/www'               # Manage directory
user 'deploy'                      # Manage user
group 'webadmins'                  # Manage group
template 'app.conf.erb'            # Template file
cron 'daily-backup'                # Cron job
execute 'custom-command'           # Run command
git '/app/repo'                    # Clone git repo
template_file '/etc/app.conf'      # Template-based file
```

### Attributes

Attributes are variables that define node configuration. They have precedence levels.

```ruby
# attributes/default.rb

# Simple attribute
default['nginx']['version'] = '1.20.0'

# Nested attribute
default['app']['database'] = {
  host: 'localhost',
  port: 5432,
  name: 'app_db'
}

# Array attribute
default['app']['enabled_modules'] = ['auth', 'logging', 'caching']

# Using attributes in recipes
package "nginx-#{node['nginx']['version']}" do
  action :install
end

template '/etc/app.conf' do
  variables(
    db_host: node['app']['database']['host'],
    db_port: node['app']['database']['port']
  )
end
```

### Precedence Levels (Low to High)

1. `default` — Default values (lowest priority)
2. `force_default` — Override defaults
3. `normal` — User values (persisted)
4. `override` — Override normal values
5. `force_override` — Override everything
6. `automatic` — System facts (highest priority)

### Roles

Roles group recipes and attributes for specific purposes.

```ruby
# roles/webserver.rb

name 'webserver'
description 'Web server role'

run_list(
  'recipe[nginx::install]',
  'recipe[nginx::configure]',
  'recipe[ssl::install]'
)

override_attributes(
  'nginx' => {
    'worker_processes' => 4,
    'keepalive_timeout' => 65
  },
  'ssl' => {
    'cert_path' => '/etc/ssl/certs'
  }
)
```

### Environments

Environments separate infrastructure stages (dev, staging, prod).

```ruby
# environments/production.rb

name 'production'
description 'Production environment'

override_attributes(
  'app' => {
    'debug' => false,
    'log_level' => 'warn'
  },
  'nginx' => {
    'worker_processes' => 8
  },
  'database' => {
    'pool_size' => 50,
    'timeout' => 30
  }
)
```

## Chef Infra Server

Chef Infra Server is the central hub for Chef infrastructure management.

### Server Components

- **Cookbook Management** — Store and version cookbooks
- **Node Data** — Track node attributes and state
- **Policy** — Define which cookbooks run on which nodes
- **Authentication** — Secure node-to-server communication

### Chef Server Workflow

```
1. Develop cookbooks locally (Chef Workstation)
2. Upload cookbooks to Chef Server
3. Define node run-lists or policies
4. Chef Client pulls configuration from server
5. Client converges system to desired state
6. Server collects node data for reporting
```

## Chef Workstation

Chef Workstation provides tools for local development:

- **Chef Infra Client** — Converges system configuration
- **Knife** — Command-line tool for server interaction
- **Test Kitchen** — Testing framework for cookbooks
- **Cookstyle** — Ruby code analyzer (replaced Foodcritic)
- **InSpec** — Infrastructure testing framework

### Knife Commands

```bash
# Upload cookbooks to server
knife cookbook upload my_cookbook

# List cookbooks on server
knife cookbook list

# Manage nodes
knife node list
knife node show node-name

# Manage roles
knife role create webserver
knife role list

# Bootstrap new node
knife bootstrap 192.168.1.100 -x ubuntu -i ~/.ssh/id_rsa
```

## Chef Policies

Modern approach to managing cookbook versions on nodes.

```ruby
# Policyfile.rb

name 'web_server'
run_list 'recipe[nginx::install]', 'recipe[ssl]'

cookbook 'nginx', '~> 8.0'
cookbook 'ssl', '~> 2.0'

default_source :supermarket

named_run_list :with_monitoring, 'recipe[monitoring]'
```

## Chef InSpec

Infrastructure testing framework integrated with Chef.

```ruby
# test/integration/default/nginx_spec.rb

describe package('nginx') do
  it { should be_installed }
end

describe service('nginx') do
  it { should be_installed }
  it { should be_enabled }
  it { should be_running }
end

describe port(80) do
  it { should be_listening }
  its('protocols') { should include 'tcp' }
end

describe http('http://localhost') do
  its('status') { should eq 200 }
  its('body') { should match /Welcome/ }
end
```

## Test Kitchen

Testing framework for cookbook development.

```yaml
# kitchen.yml

driver:
  name: vagrant

provisioner:
  name: chef_zero

platforms:
  - name: ubuntu-20.04
  - name: centos-8

suites:
  - name: default
    run_list:
      - recipe[my_cookbook::default]
    verifier:
      inspec_tests:
        - test/integration/default
```

### Test Kitchen Commands

```bash
# Create test instances
kitchen create

# Converge (run chef)
kitchen converge

# Run tests
kitchen verify

# Destroy instances
kitchen destroy

# Single command
kitchen test
```

## Chef Automate

Dashboard and analytics platform for Chef infrastructure.

- Real-time visibility into infrastructure
- Node and compliance dashboards
- Role-based access control
- Integration with third-party systems
- Data APIs for custom reporting

## Idempotence

Chef resources are idempotent — they only make changes when needed.

```ruby
# This resource is safe to run multiple times
package 'nginx' do
  action :install
end
# First run: installs nginx
# Second run: detects nginx installed, does nothing
# Third run: same as second run

# Idempotent with guards
execute 'install-ruby' do
  command './install_ruby.sh'
  not_if { ::File.exist?('/opt/ruby/bin/ruby') }
end
# Only runs if Ruby isn't already installed
```

## Exercises

### Exercise 1: Basic Recipe
1. Create a cookbook with a recipe
2. Define resources (package, file, service)
3. Use attributes for configuration
4. Test with `kitchen test`

### Exercise 2: Role and Attributes
1. Create a role for web server
2. Define environment-specific attributes
3. Apply role to a test node
4. Verify convergence

### Exercise 3: Templates
1. Create a template for configuration file
2. Use attributes in template
3. Add notification to restart service
4. Test template rendering

### Exercise 4: Integration Tests
1. Write InSpec tests for resources
2. Create Test Kitchen configuration
3. Converge and verify
4. Test multiple platforms

### Exercise 5: Multi-Cookbook Application
1. Create multiple dependent cookbooks
2. Define run-list with multiple recipes
3. Use cookbook dependencies
4. Package and upload to server

## Common Patterns

### NTP Server Configuration

```ruby
# recipes/ntp.rb

package 'ntp' do
  action :install
end

template '/etc/ntp.conf' do
  source 'ntp.conf.erb'
  notifies :restart, 'service[ntp]'
end

service 'ntp' do
  action [:enable, :start]
end
```

### User and Sudo

```ruby
# recipes/users.rb

user 'deploy' do
  shell '/bin/bash'
  home '/home/deploy'
  manage_home true
end

group 'sudo' do
  members ['deploy']
  append true
end
```

### Application Deployment

```ruby
# recipes/deploy.rb

git '/opt/myapp' do
  repository 'https://github.com/example/myapp.git'
  revision 'main'
  action :sync
  notifies :run, 'execute[install-dependencies]'
end

execute 'install-dependencies' do
  cwd '/opt/myapp'
  command 'bundle install'
  action :nothing
end
```

## Summary

Chef provides:
- **Infrastructure as Code** — Version-controlled configuration
- **Idempotent operations** — Safe repeated runs
- **Scalable management** — Thousands of nodes
- **Flexibility** — Ruby-based DSL for complex logic
- **Integration** — Works with existing tools and cloud providers

Master Chef to automate and scale infrastructure management across your organization.
