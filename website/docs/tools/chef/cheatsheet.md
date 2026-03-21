---
title: "Chef Cheat Sheet"
sidebar_label: "Quick Reference"
description: "Knife commands, cookbook patterns, resources, roles, and environments"
sidebar_position: 2
---

# Chef Cheat Sheet

Quick reference for Chef Infra commands, resources, and common patterns.

## Installation

```bash
# Install Chef Workstation
# https://downloads.chef.io/tools/workstation

# macOS
brew install chef-workstation

# Verify
chef --version
knife --version
```

## Knife Commands

### Cookbook Management

```bash
# Create new cookbook
knife cookbook create my_cookbook

# Upload cookbook to server
knife cookbook upload my_cookbook

# Upload all cookbooks
knife cookbook upload -a

# List cookbooks on server
knife cookbook list
knife cookbook list -w  # Show with versions

# Show cookbook details
knife cookbook show my_cookbook

# Delete cookbook
knife cookbook delete my_cookbook

# Download cookbook from server
knife cookbook download my_cookbook
```

### Node Management

```bash
# List all nodes
knife node list

# Show node details
knife node show node-name

# Edit node attributes
knife node edit node-name

# Run chef-client on node (via SSH)
knife ssh 'role:webserver' 'sudo chef-client'

# Bootstrap new node
knife bootstrap 192.168.1.100 \
  -x ubuntu \
  -i ~/.ssh/id_rsa \
  -N new-webserver \
  -r 'role[webserver]'

# Delete node
knife node delete node-name

# Show node run list
knife node run_list set node-name 'recipe[nginx],role[webserver]'
```

### Role Management

```bash
# Create new role
knife role create webserver

# List roles
knife role list

# Show role
knife role show webserver

# Edit role
knife role edit webserver

# Delete role
knife role delete webserver

# Upload role from file
knife role from file roles/webserver.rb
```

### Environment Management

```bash
# Create environment
knife environment create production

# List environments
knife environment list

# Show environment
knife environment show production

# Edit environment
knife environment edit production

# Delete environment
knife environment delete production

# Upload environment from file
knife environment from file environments/production.rb
```

### SSH Execution

```bash
# Run command on nodes matching query
knife ssh 'role:webserver' 'uptime'

# SSH to specific node
knife ssh 'name:web1.example.com' 'bash'

# Run with sudo
knife ssh 'role:webserver' 'sudo systemctl restart nginx'
```

## Cookbook Structure

### Create Cookbook

```bash
chef generate cookbook my_cookbook
```

### Directory Structure

```
my_cookbook/
├── metadata.rb
├── README.md
├── recipes/
│   └── default.rb
├── attributes/
│   └── default.rb
├── templates/
│   └── app.conf.erb
├── files/
│   └── config.yaml
├── libraries/
│   └── helper.rb
├── resources/
│   └── custom.rb
└── spec/
    └── spec_helper.rb
```

## Resources

### Package

```ruby
package 'nginx' do
  action :install
end

package 'nginx' do
  version '1.20.0'
  action :install
end

package 'nginx' do
  action :remove
end

# Multiple packages
%w[nginx curl wget].each do |pkg|
  package pkg do
    action :install
  end
end
```

### Service

```ruby
service 'nginx' do
  action [:enable, :start]
end

service 'nginx' do
  supports status: true, restart: true
  action :restart
end

service 'nginx' do
  action [:stop, :disable]
end
```

### File

```ruby
file '/etc/app.conf' do
  content 'server=localhost'
  mode '0644'
  owner 'root'
  group 'root'
  action :create
end

file '/var/app.pid' do
  action :delete
end

# Manage symbolic link
link '/usr/bin/ruby' do
  to '/opt/ruby/bin/ruby'
end
```

### Directory

```ruby
directory '/var/www/html' do
  mode '0755'
  owner 'www-data'
  recursive true
  action :create
end

directory '/tmp/old' do
  recursive true
  action :delete
end
```

### User and Group

```ruby
user 'deploy' do
  uid 1001
  gid 'developers'
  home '/home/deploy'
  shell '/bin/bash'
  manage_home true
  action :create
end

group 'developers' do
  members ['alice', 'bob']
  append true
  action :create
end

# Sudo access
sudo 'deploy' do
  user 'deploy'
  nopasswd true
  commands ['/usr/bin/systemctl']
end
```

### Template

```ruby
template '/etc/nginx/nginx.conf' do
  source 'nginx.conf.erb'
  mode '0644'
  variables(
    worker_processes: node['nginx']['worker_processes'],
    server_name: node['nginx']['server_name']
  )
  notifies :restart, 'service[nginx]'
  action :create
end
```

### Execute

```ruby
execute 'install-app' do
  command './install.sh'
  cwd '/tmp'
  user 'root'
  action :run
end

execute 'configure-app' do
  command 'make install'
  cwd '/opt/app'
  not_if 'test -f /opt/app/installed'
end
```

### Git

```ruby
git '/opt/myapp' do
  repository 'https://github.com/example/myapp.git'
  revision 'main'
  user 'deploy'
  group 'deploy'
  action :sync
end
```

### Cron

```ruby
cron 'daily-backup' do
  minute 0
  hour 2
  day '*'
  month '*'
  weekday '*'
  command '/usr/local/bin/backup.sh'
  action :create
end

cron 'cleanup' do
  minute '*/30'  # Every 30 minutes
  command '/usr/local/bin/cleanup.sh'
end

cron 'remove-job' do
  command '/usr/local/bin/job.sh'
  action :delete
end
```

### Ruby Block

```ruby
ruby_block 'install-app' do
  block do
    # Ruby code here
    File.write('/etc/app.conf', 'config=value')
  end
  action :run
end
```

## Recipes

### Basic Recipe

```ruby
# recipes/default.rb

# Update packages
execute 'update-packages' do
  command 'apt-get update'
  action :run
end

# Install packages
%w[nginx curl wget].each do |pkg|
  package pkg do
    action :install
  end
end

# Create user
user 'www-data' do
  home '/var/www'
  shell '/usr/sbin/nologin'
  manage_home true
end

# Create directories
%w[/var/www /var/log/app].each do |dir|
  directory dir do
    mode '0755'
    recursive true
  end
end

# Template configuration
template '/etc/nginx/nginx.conf' do
  source 'nginx.conf.erb'
  variables(
    worker_processes: node['cpu']['total'],
    server_name: node['hostname']
  )
  notifies :restart, 'service[nginx]'
end

# Start service
service 'nginx' do
  action [:enable, :start]
end
```

## Attributes

### Define Attributes

```ruby
# attributes/default.rb

# Simple attributes
default['app']['version'] = '1.0.0'
default['app']['port'] = 8080
default['app']['debug'] = false

# Nested attributes
default['database'] = {
  'host' => 'localhost',
  'port' => 5432,
  'name' => 'app_db'
}

# Array attributes
default['app']['modules'] = ['auth', 'logging']

# Use node attributes in recipes
ruby_block 'show-attributes' do
  block do
    puts "App version: #{node['app']['version']}"
    puts "Database host: #{node['database']['host']}"
  end
end
```

### Attribute Precedence

```ruby
# In attributes/default.rb
default['app']['version'] = '1.0'         # Lowest precedence
force_default['app']['version'] = '1.1'

# In recipe
normal['app']['version'] = '2.0'          # User-set
override['app']['version'] = '3.0'        # Precedence
force_override['app']['version'] = '4.0'  # Highest precedence

# Automatic (from Ohai)
node['os']           # OS
node['hostname']     # Hostname
node['cpu']['total'] # CPU count
node['ipaddress']    # IP address
```

## Roles

### Define Role

```ruby
# roles/webserver.rb

name 'webserver'
description 'Web server role'
default_attributes(
  'nginx' => {
    'worker_processes' => 4,
    'keepalive_timeout' => 65
  }
)

override_attributes(
  'app' => {
    'log_level' => 'info'
  }
)

run_list(
  'recipe[nginx::install]',
  'recipe[nginx::configure]',
  'recipe[ssl::install]'
)
```

### Apply Role to Node

```bash
# Set node run list with role
knife node run_list set webserver 'role[webserver]'

# Add role to existing run list
knife node run_list add webserver 'role[monitoring]'

# Remove role
knife node run_list remove webserver 'role[old-role]'
```

## Environments

### Define Environment

```ruby
# environments/production.rb

name 'production'
description 'Production environment'

default_attributes(
  'app' => {
    'debug' => false
  }
)

override_attributes(
  'database' => {
    'pool_size' => 50,
    'timeout' => 30
  },
  'nginx' => {
    'worker_processes' => 8
  }
)
```

### Use Environment

```bash
# Upload environment
knife environment from file environments/production.rb

# Set node environment
knife node run_list set webserver -E production

# Show environment
knife environment show production
```

## Templates

### Create Template

```erb
<!-- templates/nginx.conf.erb -->

user <%= node['nginx']['user'] %>;
worker_processes <%= node['cpu']['total'] %>;
error_log /var/log/nginx/error.log;

http {
  keepalive_timeout <%= node['nginx']['keepalive_timeout'] %>;

  <% if node['app']['gzip'] %>
  gzip on;
  <% end %>

  <% node['app']['servers'].each do |server| %>
  upstream backend_<%= server['name'] %> {
    server <%= server['address'] %>:<%= server['port'] %>;
  }
  <% end %>
}
```

### Use Template in Recipe

```ruby
template '/etc/nginx/nginx.conf' do
  source 'nginx.conf.erb'
  owner 'root'
  group 'root'
  mode '0644'
  variables(
    user: 'www-data',
    worker_processes: node['cpu']['total']
  )
  notifies :restart, 'service[nginx]'
end
```

## Notifications

### Notifies

```ruby
# Restart service when file changes
template '/etc/app.conf' do
  source 'app.conf.erb'
  notifies :restart, 'service[app]'
end

# Delayed notification (at end of chef run)
template '/etc/app.conf' do
  source 'app.conf.erb'
  notifies :restart, 'service[app]', :delayed
end

# Immediate notification (right away)
template '/etc/app.conf' do
  source 'app.conf.erb'
  notifies :restart, 'service[app]', :immediately
end
```

## Conditional Execution

### Guards

```ruby
# Not if
execute 'install-app' do
  command './install.sh'
  not_if { ::File.exist?('/opt/app') }
end

# Only if
execute 'install-app' do
  command './install.sh'
  only_if 'test -d /tmp/app'
end

# Check command output
execute 'install-app' do
  command './install.sh'
  not_if 'which app'
end
```

### Platform-Specific

```ruby
case node['platform_family']
when 'debian'
  package 'nginx' do
    action :install
  end
when 'rhel'
  package 'nginx' do
    action :install
  end
end

if node['platform'] == 'ubuntu'
  execute 'apt-get update'
end
```

## Cookstyle (Linting)

```bash
# Check cookbook style
cookstyle my_cookbook

# Fix issues automatically
cookstyle my_cookbook -a

# Set target Chef version
# In .rubocop.yml:
# ChefModernize:
#   TargetChefVersion: 17
```

## Test Kitchen

### Configuration

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

### Commands

```bash
kitchen create        # Create instances
kitchen converge      # Run chef
kitchen verify        # Run tests
kitchen destroy       # Destroy instances
kitchen test          # Full cycle
kitchen login         # SSH to instance
```

## Common Patterns

### Web Server Stack

```ruby
%w[nginx php-fpm mysql-server].each do |pkg|
  package pkg do
    action :install
  end
end

%w[nginx php-fpm mysql].each do |svc|
  service svc do
    action [:enable, :start]
  end
end
```

### Application Deployment

```ruby
git '/var/www/app' do
  repository 'https://github.com/example/app.git'
  revision 'main'
  action :sync
  notifies :run, 'execute[deploy-app]'
end

execute 'deploy-app' do
  cwd '/var/www/app'
  command 'bundle install && bundle exec rake db:migrate'
  action :nothing
end
```

### Configuration Management

```ruby
%w[app.conf app.yml].each do |file|
  template "/etc/app/#{file}" do
    source "#{file}.erb"
    variables(
      env: node.environment,
      version: node['app']['version']
    )
    notifies :restart, 'service[app]'
  end
end
```

## Tips

1. **Use attributes** — Make recipes configurable
2. **Write idempotent recipes** — Safe to run multiple times
3. **Test with Test Kitchen** — Verify recipes on real systems
4. **Use roles** — Group related recipes
5. **Version cookbooks** — Track changes
6. **Document** — README and metadata are essential
7. **Use guards** — Avoid unnecessary changes

## Summary

Chef Infra provides infrastructure automation through:
- **Recipes** — Define configuration as code
- **Roles** — Group recipes and attributes
- **Environments** — Separate staging and production
- **Knife** — Command-line interaction with Chef Server
- **Testing** — Test Kitchen and InSpec

Master Chef to automate infrastructure at scale.
