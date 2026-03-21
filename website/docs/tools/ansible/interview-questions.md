---
title: Ansible Interview Questions
sidebar_label: Interview Questions
sidebar_position: 3
---

# Ansible Interview Questions

## Beginner Level

### 1. What is Ansible and what are its main features?

**Answer**: Ansible is an open-source IT automation and configuration management tool built on Python. Main features:
- Agentless architecture (uses SSH/WinRM)
- Simple YAML-based syntax
- Idempotent operations
- No dependencies on target systems
- Powerful orchestration capabilities
- Large community and extensive modules

### 2. Explain the agentless architecture of Ansible. What are its advantages?

**Answer**: Ansible doesn't require agents on target systems. It connects via SSH (Linux) or WinRM (Windows) and executes tasks remotely.

Advantages:
- Minimal dependencies on managed nodes
- Easy bootstrapping of new systems
- Reduced attack surface
- No background services consuming resources
- Lower maintenance overhead

### 3. What is an inventory in Ansible?

**Answer**: An inventory is a file that defines the hosts Ansible manages. It can be in INI or YAML format and includes:
- Host lists organized into groups
- Host variables and group variables
- Connection details (user, port, private key)
- Dynamic inventory from external sources

Example:
```ini
[webservers]
web1.example.com
web2.example.com

[all:vars]
ansible_user=ubuntu
```

### 4. What is the difference between ad-hoc commands and playbooks?

**Answer**:
- **Ad-hoc commands**: Single tasks executed on hosts without a playbook file
- **Playbooks**: YAML files defining orchestrated workflows with multiple plays and tasks

Ad-hoc is for quick tasks, playbooks are for structured automation.

### 5. What are Ansible modules?

**Answer**: Modules are units of work in Ansible that perform specific tasks. They are reusable scripts (usually Python) that can be executed on remote hosts. Examples include `apt`, `yum`, `copy`, `service`, `user`, `git`, `docker_container`, etc.

### 6. Explain the structure of an Ansible playbook.

**Answer**: A playbook contains:
```yaml
---
- name: Play name
  hosts: target_hosts
  vars:
    var1: value1

  tasks:
    - name: Task 1
      module_name:
        argument: value

    - name: Task 2
      module_name:
        argument: value
```

Components: plays, tasks, modules, variables, handlers.

### 7. What are Ansible roles and why are they useful?

**Answer**: Roles are a way to organize playbooks into reusable, self-contained components. Structure:
- tasks/
- handlers/
- templates/
- files/
- vars/
- defaults/
- meta/

Benefits:
- Code reusability
- Better organization
- Cleaner playbooks
- Easier maintenance
- Sharing through Ansible Galaxy

### 8. What is idempotency in Ansible?

**Answer**: Idempotency means running a task multiple times produces the same result. Ansible tasks are idempotent - running them repeatedly won't cause unintended side effects. The task only makes changes if needed.

Example: Installing a package is idempotent - installing twice doesn't cause issues.

### 9. What are Ansible handlers?

**Answer**: Handlers are tasks triggered by notifications from other tasks. They run only when notified and at the end of the play.

```yaml
tasks:
  - name: Copy config
    copy:
      src: app.conf
      dest: /etc/app/conf
    notify: restart app

handlers:
  - name: restart app
    service:
      name: app
      state: restarted
```

### 10. How do you manage sensitive data in Ansible?

**Answer**: Using Ansible Vault:
```bash
# Create encrypted file
ansible-vault create secrets.yml

# Run playbook with vault
ansible-playbook site.yml --ask-vault-pass

# Encrypt string
ansible-vault encrypt_string 'secret_value'
```

Vault encrypts sensitive files and variables.

## Intermediate Level

### 11. Explain the difference between `copy` and `template` modules.

**Answer**:
- **copy**: Copies static files as-is
- **template**: Processes Jinja2 templates, replacing variables before copying

Use `template` when you need dynamic content with variables.

### 12. What are Jinja2 templates in Ansible?

**Answer**: Jinja2 is a Python templating engine used in Ansible for:
- Variable substitution: `{{ variable }}`
- Filters: `{{ variable | upper }}`
- Conditionals: `{% if condition %} ... {% endif %}`
- Loops: `{% for item in list %} ... {% endfor %}`

Templates are processed before copying to remote hosts.

### 13. How does variable precedence work in Ansible?

**Answer**: Variables are resolved in order (highest to lowest priority):
1. Extra vars (`-e` flag)
2. Task vars
3. Block vars
4. Play vars
5. Role vars
6. Host facts
7. Inventory host vars
8. Inventory group vars
9. Role defaults

### 14. Explain the difference between `include_tasks` and `import_tasks`.

**Answer**:
- **import_tasks**: Processed at playbook parse time (static)
- **include_tasks**: Processed at task execution time (dynamic)

Use `import_tasks` for static includes, `include_tasks` when you need conditionals or loops.

### 15. What are Ansible facts and how do you use them?

**Answer**: Facts are system information gathered automatically using the `setup` module. They include:
- Hostname, OS family, distribution
- IP addresses, network info
- CPU count, memory
- Installed packages

Accessed as:
```yaml
{{ ansible_hostname }}
{{ ansible_os_family }}
{{ ansible_default_ipv4.address }}
```

### 16. How do you handle errors in Ansible playbooks?

**Answer**: Using blocks:
```yaml
- block:
    - name: Task that might fail
      command: /bin/false
  rescue:
    - name: Handle error
      debug:
        msg: "Error occurred"
  always:
    - name: Cleanup
      debug:
        msg: "Always run"
```

Also: `ignore_errors`, `failed_when`, `changed_when`.

### 17. What is the `register` keyword used for?

**Answer**: `register` captures task output for later use:

```yaml
- name: Get file info
  stat:
    path: /etc/nginx.conf
  register: nginx_file

- name: Use registered var
  debug:
    msg: "File exists: {{ nginx_file.stat.exists }}"
```

### 18. Explain Ansible Galaxy and its uses.

**Answer**: Ansible Galaxy is a repository for community roles and collections.

Commands:
```bash
# Install role
ansible-galaxy role install geerlingguy.docker

# Install from requirements
ansible-galaxy role install -r requirements.yml

# Install collection
ansible-galaxy collection install community.general
```

Benefits: Reusable automation, saves time, community contributions.

### 19. How do you optimize Ansible playbook performance?

**Answer**: Optimization techniques:
- Set `gather_facts: no` if facts not needed
- Use `async` for long-running tasks
- Increase forks: `ansible-playbook -f 20`
- Use `tags` to skip unnecessary tasks
- Use `diff` mode selectively
- Avoid `shell` module when `command` works
- Cache facts: `gathering: smart`

### 20. What are conditional tasks in Ansible?

**Answer**: Using `when` clause to execute tasks conditionally:

```yaml
- name: Install nginx
  apt:
    name: nginx
  when: ansible_os_family == "Debian"

- name: Multiple conditions
  debug:
    msg: "Running"
  when:
    - inventory_hostname in groups['webservers']
    - not ansible_check_mode
```

## Intermediate-Advanced Level

### 21. Explain the difference between dynamic and static inventory.

**Answer**:
- **Static inventory**: Plain files (INI/YAML)
- **Dynamic inventory**: Scripts or plugins that generate inventory from external sources (AWS, Kubernetes, etc.)

Dynamic inventory is useful for cloud environments where hosts change frequently.

### 22. What are Ansible strategies and how do they affect playbook execution?

**Answer**: Strategies control how plays/tasks are executed:
- **linear** (default): Tasks run sequentially on all hosts
- **free**: Hosts proceed independently without waiting
- **serial**: Execute on batches of hosts
- **debug**: Stop on each task for debugging

```yaml
---
- name: Deploy
  hosts: all
  strategy: free
  tasks:
    - name: Task
      command: /bin/true
```

### 23. How do you create and use custom Ansible modules?

**Answer**: Custom modules are Python scripts in library directories:
```
library/
  my_module.py
```

Structure:
```python
from ansible.module_utils.basic import AnsibleModule

def main():
    module = AnsibleModule(
        argument_spec=dict(
            name=dict(required=True)
        )
    )
    module.exit_json(changed=False, msg="Success")

if __name__ == '__main__':
    main()
```

### 24. What is Ansible Vault and how do you use it for secrets management?

**Answer**: Vault encrypts sensitive files and strings:

```bash
# Create encrypted file
ansible-vault create secrets.yml

# Edit encrypted file
ansible-vault edit secrets.yml

# Run with vault password
ansible-playbook site.yml --vault-password-file=/path/to/pass

# Encrypt string in playbook
{{ 'secret' | vault_encrypt }}
```

Best practices: Store vault password in secure location, use separate vault files per environment.

### 25. Explain the difference between `with_*` loops and `loop` keyword.

**Answer**:
- **with_\***: Older loop syntax (with_items, with_dict, etc.)
- **loop**: New recommended syntax (Ansible 2.5+)

```yaml
# Old style
- debug:
    msg: "{{ item }}"
  with_items:
    - a
    - b

# New style
- debug:
    msg: "{{ item }}"
  loop:
    - a
    - b
```

`loop` is cleaner and preferred.

### 26. What are Ansible blocks and their use cases?

**Answer**: Blocks group tasks and apply directives to all:

```yaml
- block:
    - name: Task 1
      command: /bin/true
    - name: Task 2
      command: /bin/true
  when: condition
  become: yes
  environment:
    ENV_VAR: value
```

Use cases:
- Apply `when`, `become` to multiple tasks
- Error handling (block/rescue/always)
- Grouping related tasks

### 27. How do you handle large deployments across many hosts efficiently?

**Answer**: Techniques:
- Use `serial` for batched execution
- Increase `forks` for parallelism
- Use `async` for long-running tasks
- Implement `batch` mode to deploy in waves
- Use `delegate_to` for orchestration tasks
- Cache facts: `gathering: smart`

Example:
```yaml
- name: Deploy in batches
  hosts: all
  serial: 10
  tasks:
    - name: Deploy app
      command: /opt/deploy.sh
```

### 28. What is the difference between `pre_tasks`, `tasks`, and `post_tasks`?

**Answer**:
- **pre_tasks**: Run before handlers for imported roles
- **tasks**: Main task execution
- **post_tasks**: Run after main tasks and handlers

Execution order:
1. pre_tasks
2. roles
3. tasks
4. handlers
5. post_tasks

### 29. Explain Ansible Vault for multi-environment secrets.

**Answer**: Managing secrets across environments:

```bash
# Different vaults per environment
ansible-vault create group_vars/prod/vault.yml
ansible-vault create group_vars/dev/vault.yml

# Run with specific vault
ansible-playbook site.yml --vault-id prod@prompt

# Use in playbook
db_password: "{{ vault_db_password }}"
```

### 30. How do you use `set_fact` and `set_variable` effectively?

**Answer**: `set_fact` creates play-level variables:

```yaml
- name: Set facts
  set_fact:
    computed_var: "{{ ansible_hostname }}_prod"
    flag: true

- name: Use fact
  debug:
    msg: "{{ computed_var }}"

# Cacheable facts persist across plays
- name: Set cacheable fact
  set_fact:
    my_fact: "value"
    cacheable: yes
```

## Advanced Level

### 31. Describe Ansible Tower/AWX and its features.

**Answer**: Ansible Tower (commercial) / AWX (open-source) provides:
- Web UI for Ansible automation
- Role-based access control (RBAC)
- Job scheduling and notifications
- API for integration
- Credentials management
- Logging and auditing
- Workflow orchestration

### 32. How do you implement GitOps with Ansible?

**Answer**: GitOps practice using Ansible:
- Store playbooks in Git
- Use pull-based deployment
- Version control for all changes
- Automated testing and validation
- Merge requests for changes

```bash
# CI/CD pipeline checks
ansible-playbook site.yml --syntax-check
ansible-playbook site.yml --check
```

### 33. Explain how to use Ansible with Kubernetes.

**Answer**: Ansible can manage Kubernetes:

```yaml
- name: Deploy to Kubernetes
  hosts: localhost
  gather_facts: no
  tasks:
    - name: Apply manifest
      kubernetes.core.k8s:
        state: present
        definition: "{{ lookup('template', 'deployment.yml.j2') }}"
```

Modules: `kubernetes.core.k8s`, `kubernetes.core.helm`, `kubernetes.core.k8s_info`.

### 34. What are Ansible plugins and how do you create custom ones?

**Answer**: Plugins extend Ansible functionality:
- Inventory plugins (dynamic sources)
- Callback plugins (output formatting)
- Filter plugins (custom filters)
- Lookup plugins (data retrieval)

Custom plugin example:
```python
# plugins/filter_plugins/custom_filters.py
def my_filter(value):
    return value.upper()

class FilterModule:
    def filters(self):
        return {'my_filter': my_filter}
```

### 35. How do you implement infrastructure as code principles with Ansible?

**Answer**: IaC best practices:
- Version control all automation
- Use roles for reusability
- Implement testing (ansible-test, molecule)
- Document everything
- Use consistent naming conventions
- Implement idempotency
- Use version control for variables

Example structure:
```
ansible-iac/
  inventory/
    prod.yml
    dev.yml
  playbooks/
    site.yml
    deploy.yml
  roles/
    common/
    webserver/
    database/
  tests/
  .gitignore
  README.md
```

---

## Bonus Questions

### 36. How do you handle secrets in CI/CD with Ansible?

**Answer**:
- Use CI/CD secrets: Store vault password in CI/CD provider
- Use cloud secret managers: AWS Secrets Manager, Azure Key Vault
- Implement dynamic inventory for temporary credentials
- Use Ansible Vault with CI/CD pipelines

```bash
# In CI/CD
ansible-playbook site.yml --vault-id $VAULT_PASSWORD
```

### 37. Explain monitoring and logging in Ansible.

**Answer**: Monitoring strategies:
- Enable logging: Set `log_path` in ansible.cfg
- Use callbacks: `log_plays`, `profile_tasks`
- Integration with monitoring tools: Prometheus, DataDog
- Audit logging in AWX/Tower
- Custom reporting using registered variables

### 38. How do you test Ansible playbooks?

**Answer**: Testing approaches:
- **ansible-playbook --check**: Dry-run mode
- **ansible-test**: Built-in testing framework
- **Molecule**: Test roles in isolation with Docker/VMs
- **ansible-lint**: Lint playbooks for best practices
- **Python unittest**: For custom modules

---

## Key Concepts Summary

- **Agentless**: SSH-based execution, no agents needed
- **Idempotent**: Safe to run repeatedly
- **Declarative**: Describe desired state, not steps
- **Roles**: Reusable automation components
- **Handlers**: Event-driven task execution
- **Jinja2**: Template syntax for dynamic content
- **Vault**: Secrets encryption
- **Galaxy**: Community modules repository
- **Playbooks**: YAML-based automation workflows
- **Modules**: Reusable task units
