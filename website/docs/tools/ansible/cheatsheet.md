---
title: Ansible Cheat Sheet
sidebar_label: Cheat Sheet
sidebar_position: 2
---

# Ansible Cheat Sheet

## Installation

```bash
# Ubuntu/Debian
sudo apt-get install ansible

# CentOS/RHEL
sudo yum install ansible

# macOS
brew install ansible

# Python pip
pip install ansible

# Check version
ansible --version
```

## Ad-Hoc Commands

### Basic Syntax

```bash
ansible [pattern] -i [inventory] -m [module] -a [arguments]
```

### Common Examples

```bash
# Ping all hosts
ansible all -i inventory.ini -m ping

# Ping specific group
ansible webservers -i inventory.ini -m ping

# Run command on hosts
ansible all -i inventory.ini -m shell -a "whoami"
ansible all -i inventory.ini -m command -a "ls /home"

# Install packages
ansible all -i inventory.ini -m apt -a "name=git state=present"

# Copy files
ansible all -i inventory.ini -m copy -a "src=/local/file dest=/remote/path"

# Manage services
ansible webservers -i inventory.ini -m service -a "name=nginx state=started"

# Gather facts about hosts
ansible all -i inventory.ini -m setup
ansible all -i inventory.ini -m setup -a "filter=ansible_os_family"

# Get network info
ansible all -i inventory.ini -m setup -a "filter=ansible_default_ipv4"

# Synchronize files
ansible all -i inventory.ini -m synchronize -a "src=/local dest=/remote"

# Run with privilege escalation
ansible all -i inventory.ini -m apt -a "name=git state=present" --become

# Run as specific user
ansible all -i inventory.ini -m command -a "id" -u remoteuser

# Check mode (dry-run)
ansible all -i inventory.ini -m apt -a "name=git state=present" --check
```

## Playbook Commands

```bash
# Run playbook
ansible-playbook -i inventory.ini site.yml

# Run with verbose output
ansible-playbook -i inventory.ini site.yml -v
ansible-playbook -i inventory.ini site.yml -vv
ansible-playbook -i inventory.ini site.yml -vvv

# Dry-run (check mode)
ansible-playbook -i inventory.ini site.yml --check

# Run specific tags
ansible-playbook -i inventory.ini site.yml --tags "web,db"

# Skip specific tags
ansible-playbook -i inventory.ini site.yml --skip-tags "slow"

# Run specific hosts
ansible-playbook -i inventory.ini site.yml --limit webservers

# Run single task
ansible-playbook -i inventory.ini site.yml --start-at-task "Install nginx"

# List all tasks
ansible-playbook -i inventory.ini site.yml --list-tasks

# List all hosts
ansible-playbook -i inventory.ini site.yml --list-hosts

# Syntax check
ansible-playbook -i inventory.ini site.yml --syntax-check

# Extra variables
ansible-playbook -i inventory.ini site.yml -e "env=production"
ansible-playbook -i inventory.ini site.yml -e "var1=value1 var2=value2"

# Extra variables from file
ansible-playbook -i inventory.ini site.yml -e "@vars.yml"

# Step through playbook
ansible-playbook -i inventory.ini site.yml --step

# Diff mode (show file changes)
ansible-playbook -i inventory.ini site.yml --diff
```

## Inventory Commands

```bash
# List all hosts in inventory
ansible-inventory -i inventory.ini --list

# List hosts in YAML format
ansible-inventory -i inventory.ini --list -y

# Get host info
ansible-inventory -i inventory.ini --host hostname

# Validate inventory
ansible-inventory -i inventory.ini --parse

# List all groups
ansible all -i inventory.ini --list-hosts
```

## Vault Commands

```bash
# Create encrypted file
ansible-vault create secrets.yml

# Edit encrypted file
ansible-vault edit secrets.yml

# View encrypted file
ansible-vault view secrets.yml

# Encrypt existing file
ansible-vault encrypt secrets.yml

# Decrypt file
ansible-vault decrypt secrets.yml

# Rekey vault file (change password)
ansible-vault rekey secrets.yml

# Run playbook with vault
ansible-playbook -i inventory.ini site.yml --ask-vault-pass
ansible-playbook -i inventory.ini site.yml --vault-password-file=/path/to/pass

# Encrypt string
ansible-vault encrypt_string --vault-id @prompt 'secret_value'
```

## Galaxy Commands

```bash
# Install role
ansible-galaxy role install username.rolename

# Install with specific version
ansible-galaxy role install username.rolename,1.0.0

# Install from requirements file
ansible-galaxy role install -r requirements.yml

# Install collection
ansible-galaxy collection install community.general

# Create new role
ansible-galaxy role init my_role

# Remove role
ansible-galaxy role remove username.rolename

# List installed roles
ansible-galaxy role list

# Search for roles
ansible-galaxy search nginx

# Role info
ansible-galaxy role info username.rolename

# Build role tarball
ansible-galaxy role build my_role
```

## Ansible Configuration

### ansible.cfg

```ini
[defaults]
inventory = ./inventory.ini
host_key_checking = False
remote_user = ubuntu
private_key_file = ~/.ssh/id_rsa
roles_path = ./roles
collections_paths = ./collections
log_path = ./ansible.log
```

### Environment Variables

```bash
# Override inventory
export ANSIBLE_INVENTORY=./hosts

# Disable host key checking
export ANSIBLE_HOST_KEY_CHECKING=False

# Set remote user
export ANSIBLE_REMOTE_USER=ubuntu

# Set vault password file
export ANSIBLE_VAULT_PASSWORD_FILE=./vault_pass.txt
```

## Common Modules Reference

### Package Management

| Module | OS | Purpose |
|--------|----|---------|
| `apt` | Debian/Ubuntu | Install/remove packages |
| `yum` | RedHat/CentOS | Install/remove packages |
| `dnf` | Fedora | Install/remove packages |
| `pacman` | Arch | Install/remove packages |
| `brew` | macOS | Install/remove packages |
| `pip` | Python | Install Python packages |

**Example**:
```yaml
- name: Install package
  apt:
    name: nginx
    state: present
    update_cache: yes
```

### File Operations

| Module | Purpose |
|--------|---------|
| `copy` | Copy files to remote |
| `file` | Create/delete/modify files/dirs |
| `lineinfile` | Edit specific lines in files |
| `stat` | Get file attributes |
| `find` | Find files matching criteria |
| `synchronize` | Sync files with rsync |

**Example**:
```yaml
- name: Create directory
  file:
    path: /var/www/app
    state: directory
    owner: www-data
    group: www-data
    mode: '0755'
```

### System Management

| Module | Purpose |
|--------|---------|
| `user` | Create/modify/remove users |
| `group` | Create/modify/remove groups |
| `service` | Manage services/daemons |
| `systemd` | Manage systemd units |
| `cron` | Manage cron jobs |
| `hostname` | Set system hostname |

**Example**:
```yaml
- name: Create user
  user:
    name: appuser
    groups: sudo
    shell: /bin/bash
    home: /home/appuser
    state: present
```

### Command Execution

| Module | Purpose |
|--------|---------|
| `command` | Execute commands (no shell) |
| `shell` | Execute shell commands |
| `raw` | Execute raw SSH commands |
| `script` | Run local script on remote |
| `expect` | Handle interactive prompts |

**Example**:
```yaml
- name: Run command
  command: /usr/bin/somecommand arg1 arg2
  register: cmd_output

- name: Run shell command
  shell: |
    #!/bin/bash
    for i in {1..5}; do
      echo "Iteration $i"
    done
```

### Template & Copy

| Module | Purpose |
|--------|---------|
| `template` | Deploy Jinja2 templates |
| `copy` | Copy files to remote |
| `get_url` | Download files from URL |
| `unarchive` | Extract archive files |

**Example**:
```yaml
- name: Deploy template
  template:
    src: nginx.conf.j2
    dest: /etc/nginx/nginx.conf
    owner: root
    group: root
    mode: '0644'
  notify: restart nginx
```

### Git Operations

| Module | Purpose |
|--------|---------|
| `git` | Clone/pull git repositories |

**Example**:
```yaml
- name: Clone repository
  git:
    repo: https://github.com/user/repo.git
    dest: /opt/app
    version: main
    update: yes
```

### Docker Operations

| Module | Purpose |
|--------|---------|
| `docker_image` | Manage Docker images |
| `docker_container` | Manage Docker containers |
| `docker_network` | Manage Docker networks |
| `docker_volume` | Manage Docker volumes |

**Example**:
```yaml
- name: Run Docker container
  docker_container:
    name: myapp
    image: nginx:latest
    state: started
    ports:
      - "8080:80"
    volumes:
      - /var/www/html:/usr/share/nginx/html:ro
```

### Debugging

| Module | Purpose |
|--------|---------|
| `debug` | Print debug messages |
| `pause` | Pause playbook execution |
| `wait_for` | Wait for condition |
| `assert` | Assert conditions |

**Example**:
```yaml
- name: Debug variable
  debug:
    var: my_variable

- name: Print message
  debug:
    msg: "The user is {{ ansible_user_id }}"

- name: Wait for port
  wait_for:
    port: 8080
    delay: 10
    timeout: 300
```

## Playbook Patterns

### Register Variable

```yaml
- name: Get file info
  stat:
    path: /etc/nginx/nginx.conf
  register: nginx_conf

- name: Use registered variable
  debug:
    msg: "File exists: {{ nginx_conf.stat.exists }}"
```

### Set Facts

```yaml
- name: Set fact
  set_fact:
    my_var: "value"
    another_var: "{{ some_variable | upper }}"

- name: Use fact
  debug:
    msg: "{{ my_var }}"
```

### Conditional Execution

```yaml
- name: Conditional task
  service:
    name: nginx
    state: restarted
  when: nginx_conf.changed

- name: Multiple conditions
  debug:
    msg: "Running"
  when:
    - ansible_os_family == "Debian"
    - ansible_distribution_major_version >= "18"
```

### Loop with Conditional

```yaml
- name: Install packages
  apt:
    name: "{{ item }}"
    state: present
  loop:
    - nginx
    - git
    - curl
  when: item != "git" or git_required
```

### Block with Error Handling

```yaml
- name: Block with error handling
  block:
    - name: Task 1
      command: /bin/true

    - name: Task 2
      command: /bin/false

  rescue:
    - name: Handle error
      debug:
        msg: "Error occurred, handling it"

  always:
    - name: Always run
      debug:
        msg: "Cleanup"
```

### Include and Import

```yaml
# Include other playbook
- name: Include other tasks
  include_tasks: other_tasks.yml

# Import playbook
- import_playbook: site.yml

# Include with variables
- name: Include with vars
  include_tasks: tasks.yml
  vars:
    var1: value1
```

### Handlers

```yaml
- name: Copy config
  copy:
    src: app.conf
    dest: /etc/app/app.conf
  notify:
    - restart app
    - send alert

handlers:
  - name: restart app
    service:
      name: app
      state: restarted

  - name: send alert
    mail:
      host: localhost
      subject: "App restarted"
      body: "Application was restarted"
```

## Useful Filters

```yaml
# String filters
{{ 'hello' | upper }}                    # HELLO
{{ 'HELLO' | lower }}                    # hello
{{ 'hello world' | title }}              # Hello World
{{ 'hello' | length }}                   # 5

# Math filters
{{ 10 | int }}
{{ 3.14 | float }}
{{ [1,2,3,4,5] | sum }}                  # 15
{{ [1,2,3,4,5] | min }}                  # 1
{{ [1,2,3,4,5] | max }}                  # 5

# List filters
{{ [1,2,3] + [4,5] }}                    # [1,2,3,4,5]
{{ ['a','b','c'] | join(',') }}          # a,b,c
{{ 'a,b,c' | split(',') }}               # ['a','b','c']
{{ [1,2,2,3,3,3] | unique }}             # [1,2,3]
{{ [1,2,3] | first }}                    # 1
{{ [1,2,3] | last }}                     # 3

# Dictionary filters
{{ dict | to_json }}
{{ json_string | from_json }}
{{ data | to_nice_yaml }}

# Default values
{{ undefined_var | default('N/A') }}
{{ undefined_var | default(10, true) }}  # With boolean flag

# Path filters
{{ '/etc/nginx/nginx.conf' | basename }}      # nginx.conf
{{ '/etc/nginx/nginx.conf' | dirname }}       # /etc/nginx
```

## Variables and Facts

### Accessing Facts

```yaml
- name: Use facts
  debug:
    msg: |
      Hostname: {{ ansible_hostname }}
      OS Family: {{ ansible_os_family }}
      Distribution: {{ ansible_distribution }}
      IP Address: {{ ansible_default_ipv4.address }}
      Memory: {{ ansible_memtotal_mb }} MB
      CPU Count: {{ ansible_processor_count }}
```

### Variable Scope

```yaml
# Play-level variables
- name: My play
  hosts: all
  vars:
    play_var: "value"

  tasks:
    # Task-level variables
    - name: Task
      debug:
        msg: "{{ play_var }}"
      vars:
        task_var: "task_value"
```

## Performance Tips

```bash
# Use gather_facts: no if facts not needed
ansible-playbook -i inventory.ini site.yml

# Increase forks for parallel execution
ansible-playbook -i inventory.ini site.yml -f 10

# Use async for long-running tasks
- name: Long task
  shell: sleep 300
  async: 300
  poll: 0
  register: long_task

- name: Check on async task
  async_status:
    jid: "{{ long_task.ansible_job_id }}"
  until: async_result.finished
  retries: 30
  delay: 10
```

## Debugging

```bash
# Enable debug logging
ansible-playbook -i inventory.ini site.yml -vvv

# Use debugger
ansible-playbook -i inventory.ini site.yml --debugger=on_failed

# Set module debug
ANSIBLE_DEBUG=True ansible-playbook site.yml
```

---

## Quick Reference Summary

- Ad-hoc: `ansible [host] -m [module] -a [args]`
- Playbook: `ansible-playbook -i [inventory] [playbook.yml]`
- Vault: `ansible-vault [create|edit|view] [file]`
- Galaxy: `ansible-galaxy [role|collection] [action]`
- Check: `--check` flag for dry-run
- Verbose: `-v`, `-vv`, `-vvv` for verbosity
- Tags: `--tags` or `--skip-tags`
- Limits: `--limit` for specific hosts
- Variables: `-e` or `--extra-vars`
