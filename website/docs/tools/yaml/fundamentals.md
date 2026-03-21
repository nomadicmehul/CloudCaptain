---
title: "YAML Fundamentals"
sidebar_label: "Fundamentals"
description: "YAML syntax, data types, scalars, sequences, mappings, anchors and aliases, and comparison with JSON and XML"
sidebar_position: 1
---

# YAML Fundamentals

YAML (YAML Ain't Markup Language) is a human-readable serialization language designed for configuration files and data exchange. Unlike its predecessor, the acronym emphasizes YAML's focus on data-oriented features rather than markup.

## What is YAML?

YAML is a data serialization language that supports multiple data types and is widely used across DevOps tools:

- **Kubernetes** — Configuration manifests for pods, services, and deployments
- **Ansible** — Playbook definitions for infrastructure automation
- **GitHub Actions** — Workflow automation syntax
- **AWS CodeBuild** — Build specifications and configurations
- **Docker Compose** — Multi-container application definitions

**Key advantages:**
- Human-readable and easily writable
- Superset of JSON (all valid JSON is valid YAML)
- Minimal syntax with clean indentation
- Rich data type support
- Wide language support across ecosystems

## Basic Syntax Rules

### File Structure

YAML files use the `.yaml` or `.yml` extension and follow these essential rules:

```yaml
---  # Document start marker (optional but recommended)
key: value  # Basic key-value pair
nested:     # Nested structure
  child_key: child_value
list:       # List structure
  - item1
  - item2
...  # Document end marker (optional)
```

**Important:** YAML is **case-sensitive** and **indentation-sensitive**. Use spaces (not tabs) for indentation.

### Comments

Comments begin with `#` and extend to the end of the line:

```yaml
# This is a comment
name: John Doe  # Inline comment
age: 30         # Another inline comment
```

## Data Types

YAML supports a rich set of data types:

### Scalars (Simple Values)

**Strings:**
```yaml
# Unquoted strings
name: John Doe
city: New York

# Quoted strings (single or double)
title: "Software Engineer"
quote: 'It\'s a "great" tool'

# Multi-line strings
description: |
  This is a multi-line string
  that preserves line breaks
  (literal block scalar)

summary: >
  This is a multi-line string
  that folds long lines into
  single lines
  (folded block scalar)
```

**Numbers:**
```yaml
# Integers
count: 42
negative: -15

# Floats
price: 19.99
percentage: 0.95

# Scientific notation
large_number: 1.23e3
```

**Booleans:**
```yaml
enabled: true
disabled: false

# Alternative syntax
active: yes
inactive: no
on: true
off: false
```

**Null values:**
```yaml
value: null
empty:  # Implicitly null
another: ~  # Alternative null syntax
```

**Dates and timestamps:**
```yaml
date: 2025-03-21
timestamp: 2025-03-21T14:30:00Z
iso8601: 2025-03-21T14:30:00+00:00
```

## Collections

### Sequences (Lists/Arrays)

**Block style (indented):**
```yaml
skills:
  - Docker
  - Kubernetes
  - Terraform
  - Ansible

languages:
  - name: Python
    experience: 5
  - name: Go
    experience: 2
```

**Flow style (compact):**
```yaml
skills: [Docker, Kubernetes, Terraform, Ansible]
coordinates: [10, 20, 30]
```

### Mappings (Dictionaries/Objects)

**Block style:**
```yaml
user:
  name: John Doe
  age: 30
  city: New York
  skills:
    - Docker
    - Kubernetes

database:
  host: localhost
  port: 5432
  credentials:
    username: admin
    password: secret
```

**Flow style:**
```yaml
user: {name: John Doe, age: 30, city: New York}
point: {x: 10, y: 20}
```

### Complex Nested Structures

```yaml
organization:
  name: TechCorp
  departments:
    - name: Engineering
      teams:
        - name: Platform
          members:
            - Alice
            - Bob
        - name: DevOps
          members:
            - Charlie
            - Diana
    - name: Operations
      teams:
        - name: Infrastructure
          members:
            - Eve
```

## Anchors and Aliases

Anchors (`&`) and aliases (`*`) allow you to reuse content without repetition:

```yaml
# Define an anchor
defaults: &default_settings
  timeout: 30
  retries: 3
  ssl: true

# Use alias to reference the anchor
prod_config:
  <<: *default_settings
  environment: production

dev_config:
  <<: *default_settings
  environment: development
  debug: true
```

**Practical example with anchors:**
```yaml
# Base configuration
app_defaults: &app_base
  log_level: info
  cache_ttl: 3600
  max_connections: 100

services:
  api:
    <<: *app_base
    port: 8080
  worker:
    <<: *app_base
    port: 8081
    log_level: debug  # Override inherited value
```

## Multi-Document YAML

A single YAML file can contain multiple documents separated by `---`:

```yaml
---
name: config-v1
version: 1
settings:
  debug: false
---
name: config-v2
version: 2
settings:
  debug: true
  enhanced_logging: true
---
name: config-v3
version: 3
settings:
  debug: true
  enhanced_logging: true
  metrics_enabled: true
```

## Comparison with JSON

### Syntax Differences

**JSON:**
```json
{
  "name": "John Doe",
  "age": 30,
  "skills": ["Docker", "Kubernetes"],
  "address": {
    "city": "New York",
    "zip": "10001"
  }
}
```

**YAML equivalent:**
```yaml
name: John Doe
age: 30
skills:
  - Docker
  - Kubernetes
address:
  city: New York
  zip: "10001"
```

**YAML as JSON (valid YAML):**
```yaml
{
  "name": "John Doe",
  "age": 30,
  "skills": ["Docker", "Kubernetes"]
}
```

### Key Differences

| Feature | YAML | JSON |
|---------|------|------|
| **Readability** | Highly human-readable | Readable but verbose |
| **Quotes** | Often optional | Required for strings |
| **Comments** | Supported with `#` | Not supported |
| **Indentation** | Significant (required) | Not significant |
| **Null values** | Multiple syntaxes (`null`, `~`, empty) | Only `null` |
| **Type inference** | Automatic | Explicit |
| **File size** | More compact | More verbose |
| **Use case** | Configuration, data serialization | Data interchange (APIs) |

## Comparison with XML

### XML Example

```xml
<user>
  <name>John Doe</name>
  <age>30</age>
  <skills>
    <skill>Docker</skill>
    <skill>Kubernetes</skill>
  </skills>
</user>
```

### Equivalent YAML

```yaml
user:
  name: John Doe
  age: 30
  skills:
    - Docker
    - Kubernetes
```

### Comparison

| Aspect | YAML | XML |
|--------|------|-----|
| **Syntax** | Minimal, indentation-based | Tag-based with attributes |
| **Readability** | Excellent | Good, but verbose |
| **Learning curve** | Gentle | Moderate |
| **Attributes vs elements** | No distinction | Different handling |
| **File size** | Compact | Large |
| **Validation schemas** | JSON Schema | XSD |
| **DevOps usage** | Very common | Legacy systems |

## Real-World Examples

### Kubernetes Pod Manifest

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: web-server
  namespace: default
  labels:
    app: web
    tier: frontend
spec:
  containers:
  - name: nginx
    image: nginx:1.21
    ports:
    - containerPort: 80
      protocol: TCP
    resources:
      requests:
        memory: 128Mi
        cpu: 100m
      limits:
        memory: 256Mi
        cpu: 500m
    env:
    - name: ENVIRONMENT
      value: "production"
  restartPolicy: Always
```

### Ansible Playbook

```yaml
---
- name: Deploy web application
  hosts: webservers
  become: yes
  vars:
    app_version: 2.5.0
    app_port: 8080
  tasks:
    - name: Update system packages
      apt:
        update_cache: yes
        upgrade: dist
    - name: Install Python dependencies
      pip:
        name: "{{ item }}"
        state: present
      loop:
        - flask
        - flask-cors
        - gunicorn
    - name: Deploy application
      git:
        repo: https://github.com/company/app.git
        dest: /opt/app
        version: "v{{ app_version }}"
      notify: Restart application
  handlers:
    - name: Restart application
      systemd:
        name: web-app
        state: restarted
```

### GitHub Actions Workflow

```yaml
name: Deploy to Production

on:
  push:
    branches:
      - main
  pull_request:
    branches:
      - main

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - name: Checkout code
        uses: actions/checkout@v3
      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v2
      - name: Log in to container registry
        uses: docker/login-action@v2
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - name: Build and push image
        uses: docker/build-push-action@v4
        with:
          context: .
          push: true
          tags: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:latest
```

### Docker Compose Configuration

```yaml
version: '3.8'

services:
  web:
    image: nginx:latest
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./html:/usr/share/nginx/html:ro
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
    environment:
      ENVIRONMENT: production
    depends_on:
      - api
    networks:
      - app-network

  api:
    build:
      context: ./api
      dockerfile: Dockerfile
    environment:
      DATABASE_URL: postgresql://user:pass@db:5432/app
      REDIS_URL: redis://cache:6379
    depends_on:
      - db
      - cache
    networks:
      - app-network

  db:
    image: postgres:13
    environment:
      POSTGRES_USER: user
      POSTGRES_PASSWORD: password
      POSTGRES_DB: app
    volumes:
      - db_data:/var/lib/postgresql/data
    networks:
      - app-network

  cache:
    image: redis:6-alpine
    networks:
      - app-network

volumes:
  db_data:

networks:
  app-network:
    driver: bridge
```

## Common Gotchas

### Issue 1: Indentation Mistakes

**Incorrect (tabs):**
```yaml
# Using tabs instead of spaces
servers:
→ server1: prod-1  # ← This is a tab character
→ server2: prod-2
```

**Correct (spaces):**
```yaml
servers:
  server1: prod-1
  server2: prod-2
```

### Issue 2: Ambiguous Key Names

```yaml
# Dangerous: YAML interprets 'no' as boolean false
production: no  # ← Becomes boolean false, not string "no"

# Safe: Quote the string
production: "no"  # ← String value
```

### Issue 3: Colon and Dash Confusion

```yaml
# Colons in values need quoting
description: "Release: v1.0.0"  # ← Needs quotes
message: Release v1.0.0  # ← Valid without quotes

# Dashes as list items
items:
  - "- This is item one"  # ← Dash is quoted as part of value
  - Normal item
```

### Issue 4: String to Number Coercion

```yaml
# Unquoted numbers
count: 42       # ← Integer
price: 19.99    # ← Float
code: 007       # ← Octal number

# Quote to force string
zipcode: "10001"  # ← String, not number
version: "1.0"    # ← String, preserves trailing zero
```

### Issue 5: Implicit Null Values

```yaml
# These are all null
empty_key:
another_null: null
also_null: ~

# This is an empty string
empty_string: ""
```

## YAML Best Practices

1. **Use consistent indentation** — Stick to 2 or 4 spaces per level
2. **Quote ambiguous values** — Quote strings that could be booleans or numbers
3. **Use meaningful keys** — Make configuration self-documenting
4. **Document complex structures** — Add comments explaining non-obvious sections
5. **Validate your YAML** — Use `yamllint` or online validators
6. **Use anchors for DRY code** — Reduce repetition with `&` and `*`
7. **Separate concerns** — Split large files into multiple documents
8. **Handle special characters** — Quote strings containing colons, commas, or brackets

## Tools and Validation

### Online YAML Validators
- [YAML Lint](http://www.yamllint.com/)
- [OnlineYAML](https://www.onlineyaml.com/)

### Command-line Tools
```bash
# Install yamllint
pip install yamllint

# Validate YAML file
yamllint my-config.yaml
```

### Python YAML Parsing

```python
import yaml

# Read YAML file
with open('config.yaml', 'r') as f:
    data = yaml.safe_load(f)

# Write YAML file
with open('output.yaml', 'w') as f:
    yaml.dump(data, f, default_flow_style=False)

# Parse YAML string
yaml_string = """
name: John
age: 30
skills:
  - Docker
  - Kubernetes
"""
config = yaml.safe_load(yaml_string)
print(config['name'])  # Output: John
```

## Exercises

### Exercise 1: Basic Structure
Create a YAML file representing a server configuration with:
- Server name
- IP address
- Port numbers (HTTP and HTTPS)
- Enabled services (list)
- Environment variables (key-value pairs)

### Exercise 2: Nested Data
Convert the following JSON to YAML:
```json
{
  "database": {
    "primary": {
      "host": "db-1.example.com",
      "port": 5432
    },
    "replicas": [
      {"host": "db-2.example.com", "port": 5432},
      {"host": "db-3.example.com", "port": 5432}
    ]
  }
}
```

### Exercise 3: Anchors and Aliases
Create a YAML configuration for three environments (dev, staging, prod) that share common settings but have environment-specific overrides.

### Exercise 4: Multi-line Strings
Write a YAML file containing:
- A literal block scalar (preserves newlines)
- A folded block scalar (folds lines)
- A plain scalar with line wrapping

### Exercise 5: Real-world Application
Create a complete Kubernetes Pod definition with:
- Multiple containers
- Environment variables
- Resource limits and requests
- Volume mounts
- Health probes

## Summary

YAML is the lingua franca of DevOps infrastructure. Its simplicity and readability make it ideal for configuration management, while its rich type system and support for complex structures enable sophisticated applications. Understanding YAML deeply—including its quirks and best practices—is essential for anyone working with modern DevOps tools.

**Key takeaways:**
- YAML is indentation-sensitive and case-sensitive
- Strings don't always need quotes, but they should for ambiguous values
- Lists and mappings are the core collection types
- Anchors and aliases reduce repetition
- Comments enhance configuration clarity
- YAML is a superset of JSON, not a replacement
