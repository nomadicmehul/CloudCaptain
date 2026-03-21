---
title: "YAML Cheat Sheet"
sidebar_label: "Quick Reference"
description: "Quick reference for YAML syntax, DevOps configurations, and common patterns"
sidebar_position: 2
---

# YAML Cheat Sheet

A quick reference guide for YAML syntax and common DevOps configurations.

## Basic Syntax

### Key-Value Pairs
```yaml
key: value
name: John Doe
count: 42
```

### Strings
```yaml
# Unquoted
message: Hello World

# Quoted
title: "The \"Quick\" Guide"
escaped: 'It\'s great'

# Multi-line (literal - preserves newlines)
description: |
  First line
  Second line
  Third line

# Multi-line (folded - joins lines)
summary: >
  This is a very long
  sentence that spans
  multiple lines
```

### Numbers, Booleans, Null
```yaml
integer: 42
float: 3.14
boolean: true
boolean_alt: false
null_value: null
null_alt: ~
```

### Lists
```yaml
# Block style
items:
  - item1
  - item2
  - item3

# Flow style
items: [item1, item2, item3]

# Nested lists
matrix:
  - [1, 2, 3]
  - [4, 5, 6]
```

### Dictionaries
```yaml
# Block style
person:
  name: John
  age: 30
  city: NYC

# Flow style
person: {name: John, age: 30, city: NYC}
```

### Mixed Structures
```yaml
users:
  - name: Alice
    age: 28
    skills: [Python, Docker]
  - name: Bob
    age: 35
    skills: [Go, Kubernetes]
```

## Advanced Syntax

### Anchors & Aliases
```yaml
# Define anchor with &
defaults: &default_settings
  timeout: 30
  retries: 3

# Reuse with alias *
service1: &service1_ref
  <<: *default_settings
  port: 8080

service2:
  <<: *service1_ref
  port: 8081
```

### Merge Keys
```yaml
base: &base
  name: app
  version: 1.0

prod:
  <<: *base
  environment: production
  debug: false

dev:
  <<: *base
  environment: development
  debug: true
```

## Kubernetes YAML Patterns

### Pod Definition
```yaml
apiVersion: v1
kind: Pod
metadata:
  name: nginx-pod
  namespace: default
  labels:
    app: nginx
spec:
  containers:
  - name: nginx
    image: nginx:latest
    ports:
    - containerPort: 80
    resources:
      requests:
        memory: "64Mi"
        cpu: "250m"
      limits:
        memory: "128Mi"
        cpu: "500m"
```

### Deployment
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web-app
spec:
  replicas: 3
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
      - name: web
        image: myapp:1.0
        ports:
        - containerPort: 8080
        env:
        - name: DB_HOST
          value: "db.example.com"
```

### Service
```yaml
apiVersion: v1
kind: Service
metadata:
  name: web-service
spec:
  type: LoadBalancer
  selector:
    app: web
  ports:
  - protocol: TCP
    port: 80
    targetPort: 8080
```

### ConfigMap
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
data:
  app.properties: |
    server.port=8080
    app.name=MyApp
  database.yaml: |
    host: db.local
    port: 5432
```

### Secret
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: app-secret
type: Opaque
data:
  username: dXNlcm5hbWU=  # base64 encoded
  password: cGFzc3dvcmQ=  # base64 encoded
```

## Ansible Playbook Patterns

### Simple Task
```yaml
---
- name: Provision servers
  hosts: webservers
  become: yes
  tasks:
    - name: Update packages
      apt:
        update_cache: yes
        upgrade: dist

    - name: Install packages
      apt:
        name: "{{ item }}"
        state: present
      loop:
        - nginx
        - curl
        - git
```

### Variables and Handlers
```yaml
---
- name: Configure application
  hosts: app_servers
  vars:
    app_version: 2.5.0
    app_port: 8080
  tasks:
    - name: Deploy app
      git:
        repo: https://github.com/company/app.git
        dest: /opt/app
        version: "v{{ app_version }}"
      notify: Restart app

    - name: Configure firewall
      ufw:
        rule: allow
        port: "{{ app_port }}"

  handlers:
    - name: Restart app
      systemd:
        name: myapp
        state: restarted
```

### Conditionals and Loops
```yaml
---
- name: Conditional deployment
  hosts: all
  tasks:
    - name: Install Node.js on Debian
      apt:
        name: nodejs
        state: present
      when: ansible_os_family == "Debian"

    - name: Install Node.js on RedHat
      yum:
        name: nodejs
        state: present
      when: ansible_os_family == "RedHat"

    - name: Create multiple users
      user:
        name: "{{ item }}"
        state: present
      loop:
        - alice
        - bob
        - charlie
```

### Roles
```yaml
---
- name: Deploy with roles
  hosts: webservers
  roles:
    - common
    - nginx
    - app
  post_tasks:
    - name: Health check
      uri:
        url: http://localhost:80/health
        method: GET
      register: health_result
      failed_when: health_result.status != 200
```

## Docker Compose Patterns

### Basic Service Definition
```yaml
version: '3.8'

services:
  web:
    image: nginx:latest
    ports:
      - "80:80"
    volumes:
      - ./html:/usr/share/nginx/html:ro
    environment:
      - TZ=UTC
    restart: always
```

### Multi-Service with Dependencies
```yaml
version: '3.8'

services:
  web:
    build:
      context: ./web
      dockerfile: Dockerfile
    ports:
      - "3000:3000"
    depends_on:
      - api
      - db
    environment:
      API_URL: http://api:8000

  api:
    build:
      context: ./api
      dockerfile: Dockerfile
    expose:
      - "8000"
    depends_on:
      - db
      - redis
    environment:
      DATABASE_URL: postgresql://user:pass@db:5432/app
      REDIS_URL: redis://redis:6379

  db:
    image: postgres:13
    environment:
      POSTGRES_USER: user
      POSTGRES_PASSWORD: password
      POSTGRES_DB: app
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U user"]
      interval: 10s
      timeout: 5s
      retries: 5

  redis:
    image: redis:6-alpine
    expose:
      - "6379"

volumes:
  postgres_data:

networks:
  default:
    name: app-network
```

### Build Context
```yaml
services:
  api:
    build:
      context: ./api
      dockerfile: Dockerfile.prod
      args:
        BUILD_DATE: 2025-03-21
        VCS_REF: abc123def456
    image: myapp/api:1.0
```

## GitHub Actions Workflow Patterns

### Basic Workflow
```yaml
name: CI/CD Pipeline

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.10'
      - run: pip install -r requirements.txt
      - run: pytest tests/
```

### Build and Push Docker Image
```yaml
name: Build and Push Image

on:
  push:
    branches: [main]
    tags: ['v*']

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: mycompany/myapp

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v3
      - uses: docker/setup-buildx-action@v2
      - uses: docker/login-action@v2
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v4
        with:
          context: .
          push: true
          tags: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:latest
```

### Matrix Strategy
```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ['3.8', '3.9', '3.10', '3.11']
        node-version: [16, 18, 20]
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: ${{ matrix.python-version }}
      - uses: actions/setup-node@v3
        with:
          node-version: ${{ matrix.node-version }}
      - run: npm test
      - run: pytest
```

## AWS CodeBuild buildspec.yml

```yaml
version: 0.2

phases:
  pre_build:
    commands:
      - echo "Building application..."
      - echo "Installing dependencies..."
      - npm install
  build:
    commands:
      - echo "Running tests..."
      - npm test
      - echo "Building application..."
      - npm run build
  post_build:
    commands:
      - echo "Build completed"
      - aws s3 cp dist/ s3://my-bucket/build/ --recursive

artifacts:
  files:
    - dist/**/*
  name: BuildArtifact
```

## Common DevOps YAML Structures

### Configuration Management
```yaml
# Application config
app:
  name: MyApp
  version: 1.0.0
  port: 8080
  debug: false
  features:
    authentication: true
    logging: true
  database:
    host: db.example.com
    port: 5432
    pool_size: 10
    timeout: 30
```

### Infrastructure as Code Pattern
```yaml
# Terraform-like structure
infrastructure:
  region: us-east-1
  environment: production
  vpc:
    cidr: 10.0.0.0/16
    subnets:
      - name: public-1a
        cidr: 10.0.1.0/24
        az: us-east-1a
      - name: private-1a
        cidr: 10.0.10.0/24
        az: us-east-1a
  instances:
    - name: web-1
      type: t3.medium
      subnet: public-1a
    - name: app-1
      type: t3.large
      subnet: private-1a
```

### Pipeline Configuration
```yaml
pipeline:
  stages:
    - name: build
      steps:
        - script: ./scripts/build.sh
        - timeout: 300
    - name: test
      steps:
        - script: npm test
        - timeout: 600
    - name: deploy
      steps:
        - script: ./scripts/deploy.sh
        - timeout: 900
      when: branch == "main"
```

## Quick Syntax Reference

| Syntax | Meaning | Example |
|--------|---------|---------|
| `:` | Key-value separator | `key: value` |
| `-` | List item | `- item` |
| `\|` | Literal block (preserve newlines) | `description:\|` |
| `>` | Folded block (fold lines) | `summary: >` |
| `&` | Anchor definition | `defaults: &anchor` |
| `*` | Alias reference | `<<: *anchor` |
| `<<` | Merge key | `<<: *defaults` |
| `#` | Comment | `# This is a comment` |
| `---` | Document start | `---` |
| `...` | Document end | `...` |
| `~` | Null value | `value: ~` |
| `!!` | Type casting | `!!str 123` |

## Tips and Tricks

### Type Casting
```yaml
string_number: !!str 123        # Forces string
integer_from_string: !!int "456"  # Forces integer
boolean_from_string: !!bool "yes" # Forces boolean
```

### Escape Special Characters
```yaml
colon_in_string: "Release: v1.0.0"
quote_in_string: "He said \"hello\""
backslash: "Path: C:\\Users\\name"
newline_in_string: "Line 1\nLine 2"
```

### Indentation Rules
```yaml
# 2 spaces per level (recommended)
level1:
  level2:
    level3: value

# 4 spaces also valid
level1:
    level2:
        level3: value

# Tabs NOT allowed
# level1:
# →level2: WRONG - don't use tabs!
```

## Common Mistakes

| Mistake | Wrong | Correct |
|---------|-------|---------|
| Using tabs | `→key: value` | `  key: value` |
| Colon without space | `key:value` | `key: value` |
| Inconsistent indentation | Lines indent 2, then 3 spaces | All lines indent by same amount |
| Unquoted "no" | `enabled: no` | `enabled: "no"` |
| Comments in lists | `- item # comment` | `-item\n# comment` |
| Ambiguous booleans | `active: true` | `active: "true"` (if string needed) |

## Validation and Linting

```bash
# Validate with yamllint
yamllint -d relaxed config.yaml

# Validate JSON against YAML
jq . config.json | yq -r . > config.yaml

# Check Kubernetes manifests
kubectl apply -f manifest.yaml --dry-run=client

# Online validators
# - http://www.yamllint.com/
# - https://www.onlineyaml.com/
```

## Summary

YAML is essential for modern DevOps. Keep this cheat sheet handy for quick reference on syntax, Kubernetes, Ansible, Docker Compose, and GitHub Actions patterns.
