---
title: "Docker Security & Production"
sidebar_label: "Security & Production"
description: "Docker security best practices, monitoring, CI/CD pipelines, and running containers in production"
sidebar_position: 5
---

# Docker Security & Production

Running Docker securely in production requires a comprehensive approach to image security, runtime hardening, monitoring, and deployment best practices. This guide covers defensive strategies, monitoring solutions, and practical CI/CD patterns for production environments.

## Security

### Container Security Overview

Docker containers introduce a unique attack surface compared to traditional applications. While containerization provides process isolation, the shared kernel and dynamic nature of container orchestration require specific security controls.

**Threat Model:**
- **Image Supply Chain**: Compromised or outdated base images
- **Runtime Exploits**: Kernel vulnerabilities affecting the host
- **Container Escape**: Privilege escalation allowing container-to-host access
- **Lateral Movement**: One compromised container accessing others
- **Data Exposure**: Secrets in images, unencrypted storage/transit

**Defense in Depth: Security Layers**

```mermaid
graph TB
    subgraph HostLayer["Host & Kernel Layer"]
        H["Linux Kernel<br/>Namespaces, cgroups"]
    end

    subgraph DaemonLayer["Docker Daemon Layer"]
        D["Docker Daemon<br/>Access Control, TLS"]
    end

    subgraph ImageLayer["Image Layer"]
        I["Container Image<br/>Signed, Scanned,<br/>No secrets"]
    end

    subgraph RuntimeLayer["Runtime Security Layer"]
        R["Capabilities, Seccomp<br/>AppArmor, Read-only FS"]
    end

    subgraph NetworkLayer["Network Layer"]
        N["Network Policies<br/>Isolated networks,<br/>TLS encryption"]
    end

    N --> RuntimeLayer
    RuntimeLayer --> ImageLayer
    ImageLayer --> DaemonLayer
    DaemonLayer --> HostLayer

    style H fill:#1B2A4A,color:#fff
    style D fill:#1E9BD7,color:#fff
    style I fill:#38BDF8,color:#000
    style R fill:#38BDF8,color:#000
    style N fill:#06B6D4,color:#000
```

**Defense in Depth Strategy:**
- Secure the image supply chain
- Harden runtime configuration
- Implement network isolation
- Monitor container behavior
- Apply principle of least privilege across all layers

### Image Security

Image security is the foundation of container security. Compromised or vulnerable images create security debt that persists across every container instance.

**Use Official and Verified Images:**
- Pull from the Docker Official Images library (distinguished by blue badges)
- Verify digital signatures and image digests
- Check image provenance and maintenance status
- Review Dockerfile source code when possible

**Example - Verifying Image Digest:**
```bash
# Pull image with digest verification
docker pull ubuntu@sha256:a7b8ffd33d65d7f0f2d2c0d2d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0

# List images with digests
docker images --digests
```

**Vulnerability Scanning:**

Modern container registries and tools provide vulnerability scanning capabilities:

- **Docker Scout**: Native Docker platform for scanning images and providing remediation guidance
  ```bash
  docker scout cves myimage:latest
  docker scout recommendations myimage:latest
  ```

- **Trivy**: Open-source scanner detecting vulnerabilities and misconfigurations
  ```bash
  trivy image myimage:latest
  trivy image --severity HIGH,CRITICAL myimage:latest
  ```

- **Snyk**: Developer-focused vulnerability scanning with fix recommendations
  ```bash
  snyk container test myimage:latest
  ```

**Avoid the Latest Tag:**
```dockerfile
# Bad - unpredictable base image version
FROM ubuntu:latest

# Good - specific stable version
FROM ubuntu:24.04
```

Pinning versions ensures consistency and allows testing before upgrades.

**Minimal Base Images:**

Smaller images reduce attack surface and improve deployment speed:

- **Alpine Linux** (~5 MB): Minimal Linux distribution
  ```dockerfile
  FROM alpine:3.19
  RUN apk add --no-cache python3
  ```

- **Distroless Images** (Google): Contains only application + runtime
  ```dockerfile
  FROM gcr.io/distroless/python3-debian12
  COPY app.py .
  CMD ["app.py"]
  ```

- **Scratch Image** (~0 MB): Empty base for statically compiled binaries
  ```dockerfile
  FROM scratch
  COPY app /
  ENTRYPOINT ["/app"]
  ```

### Build Security

**Secrets Management - Never Store Secrets in Images:**

```dockerfile
# Bad - secrets in image layer
FROM ubuntu:24.04
RUN echo "AWS_KEY=AKIAIOSFODNN7EXAMPLE" > /app/.env

# Good - secrets via build args (only available during build)
ARG DATABASE_PASSWORD
RUN echo "Password accepted during build"

# Better - secrets via Docker BuildKit secrets
FROM ubuntu:24.04
RUN --mount=type=secret,id=db_password \
    cat /run/secrets/db_password > /tmp/setup.sql
```

**Multi-Stage Builds:**

Reduce final image size and exclude build tools:

```dockerfile
# Build stage
FROM golang:1.21 AS builder
WORKDIR /src
COPY . .
RUN go build -o app .

# Runtime stage - only includes compiled binary
FROM alpine:3.19
RUN apk add --no-cache ca-certificates
COPY --from=builder /src/app /usr/local/bin/
ENTRYPOINT ["app"]
```

Multi-stage builds eliminate build dependencies from final images.

**.dockerignore:**

Exclude unnecessary files to reduce context size and prevent secrets from being included:

```
.git
.env
.env.local
*.log
node_modules
.npm
.cache
__pycache__
*.pyc
.DS_Store
.vscode
.idea
dist/
build/
coverage/
.dockerignore
Dockerfile
.gitignore
```

**Pin Dependency Versions:**

```dockerfile
# Bad - unpredictable versions
RUN apt-get update && apt-get install -y curl

# Good - specific versions
RUN apt-get update && apt-get install -y curl=7.68.0-1ubuntu1.17
```

### Runtime Security

**Run as Non-Root User:**

Running containers as root increases blast radius if the container is compromised:

```dockerfile
# Create dedicated user
RUN groupadd -r appuser && useradd -r -g appuser appuser
USER appuser
COPY --chown=appuser:appuser . .
```

```bash
# Verify user at runtime
docker run -u 1000:1000 myapp
docker exec <container> whoami
```

**Read-Only Filesystems:**

Prevent runtime modifications by mounting root filesystem as read-only:

```bash
docker run --read-only myapp
```

For containers requiring temporary files, use tmpfs:

```bash
docker run --read-only --tmpfs /tmp:size=100m myapp
```

**Drop Unnecessary Capabilities:**

Linux capabilities provide granular privilege control. Drop what's not needed:

```bash
# Drop all capabilities, add only required ones
docker run --cap-drop ALL --cap-add NET_BIND_SERVICE myapp

# View default capabilities
docker run --rm alpine grep CapEff /proc/self/status
```

Common capability mappings:
- `NET_BIND_SERVICE`: Bind to ports < 1024
- `NET_RAW`: Raw socket creation
- `SYS_ADMIN`: Administrative operations
- `SETFCAP`: Set file capabilities

**Limit Resources:**

Prevent resource exhaustion and container escape attempts:

```bash
# CPU limits (2 CPUs)
docker run --cpus=2 myapp

# Memory limits (512 MB)
docker run --memory=512m myapp

# Memory + swap limits
docker run --memory=512m --memory-swap=1g myapp

# Combined resource limits
docker run --cpus=1.5 --memory=512m --oom-kill-disable=false myapp
```

**Seccomp Profiles:**

Seccomp restricts system calls a container can invoke:

```bash
# Use default seccomp profile
docker run --security-opt seccomp=default myapp

# Use custom seccomp profile
docker run --security-opt seccomp=/path/to/profile.json myapp
```

Example seccomp profile (whitelist common syscalls):
```json
{
  "defaultAction": "SCMP_ACT_ERRNO",
  "defaultErrnoRet": 1,
  "archMap": [{"architecture": "SCMP_ARCH_X86_64"}],
  "syscalls": [
    {
      "names": ["read", "write", "open", "close", "stat", "fstat"],
      "action": "SCMP_ACT_ALLOW"
    }
  ]
}
```

**AppArmor:**

Provide mandatory access control for containers:

```bash
# Run with AppArmor profile
docker run --security-opt apparmor=docker-default myapp

# Create custom AppArmor profile
docker run --security-opt apparmor=/path/to/profile myapp
```

### Network Security

**Use User-Defined Networks:**

Default bridge network exposes all containers to each other via DNS discovery:

```bash
# Create isolated network
docker network create --driver bridge isolated-net

# Run containers on isolated network
docker run --network isolated-net myapp
docker run --network isolated-net mydb

# Containers cannot discover services outside this network
```

**Don't Expose Unnecessary Ports:**

```dockerfile
# Bad - exposes all ports
EXPOSE 0-65535

# Good - only required ports
EXPOSE 8080
```

```bash
# Bad - publishes to all interfaces
docker run -p 5432:5432 postgres

# Good - bind to localhost only
docker run -p 127.0.0.1:5432:5432 postgres
```

**Use TLS for Docker Daemon:**

Secure communication between client and daemon:

```bash
# Generate certificates
openssl genrsa -out ca-key.pem 2048
openssl req -new -x509 -days 365 -key ca-key.pem -out ca.pem

# Configure daemon to use TLS
# /etc/docker/daemon.json
{
  "tls": true,
  "tlscert": "/etc/docker/server.pem",
  "tlskey": "/etc/docker/server-key.pem",
  "tlscacert": "/etc/docker/ca.pem"
}

# Connect with TLS
docker --tlsverify --tlscacert=ca.pem --tlscert=cert.pem --tlskey=key.pem -H=tcp://hostname:2376
```

### Docker Security Checklist

Comprehensive checklist for hardening containers in production:

- [ ] Use official or verified images from trusted registries
- [ ] Scan all images for vulnerabilities (CVEs) before deployment
- [ ] Pin specific base image versions, never use `latest` tag
- [ ] Use minimal base images (Alpine, distroless, scratch) where appropriate
- [ ] Don't store secrets, API keys, or credentials in images
- [ ] Use multi-stage builds to exclude build tools from runtime images
- [ ] Implement `.dockerignore` to prevent leaking sensitive files
- [ ] Pin all dependency versions in Dockerfile
- [ ] Run containers as non-root user with least privilege
- [ ] Mount root filesystem as read-only where possible
- [ ] Drop all Linux capabilities, add only required ones (`--cap-drop ALL`)
- [ ] Set CPU and memory resource limits (`--cpus` and `--memory`)
- [ ] Use seccomp profiles to restrict system calls
- [ ] Apply AppArmor mandatory access control profiles
- [ ] Isolate containers on user-defined networks
- [ ] Don't expose unnecessary ports, bind to localhost when possible
- [ ] Use TLS for Docker daemon communication
- [ ] Implement health checks for all containers
- [ ] Enable container logging and centralize logs
- [ ] Regularly rebuild images to patch base images and dependencies
- [ ] Use image signing and digest verification in registries
- [ ] Implement container image retention and cleanup policies
- [ ] Monitor containers for behavioral anomalies and runtime violations

## Monitoring

### Container Monitoring Challenges

Container monitoring differs from traditional VM/server monitoring due to:

- **Ephemeral Nature**: Containers are created and destroyed frequently, making historical correlation difficult
- **Scale**: Orchestrated environments run hundreds or thousands of containers
- **Dynamic Environments**: Container IPs, ports, and locations change constantly
- **Resource Sharing**: Multiple containers share kernel and resources, requiring careful metric interpretation
- **Log Volume**: Container logs grow rapidly at scale without proper rotation and centralization

Effective monitoring requires container-aware tooling that understands orchestration platforms and container lifecycle.

### Monitoring Tools

**Docker Stats (Native):**

Built-in container statistics from the Docker daemon:

```bash
# Real-time container stats
docker stats

# Specific container
docker stats myapp

# No-stream mode (single snapshot)
docker stats --no-stream myapp

# Show container names
docker stats --format "{{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}"
```

Output includes CPU %, memory usage, network I/O, and block I/O.

**cAdvisor (Container Advisor):**

Google's container monitoring tool providing detailed resource metrics:

```bash
# Run cAdvisor (exposes metrics on port 8080)
docker run \
  --volume=/:/rootfs:ro \
  --volume=/var/run:/var/run:ro \
  --volume=/sys:/sys:ro \
  --volume=/var/lib/docker/:/var/lib/docker:ro \
  --detach=true \
  --name=cadvisor \
  --publish=8080:8080 \
  gcr.io/cadvisor/cadvisor:latest
```

Access metrics at `http://localhost:8080`

**Prometheus + Grafana:**

Industry-standard monitoring stack for container environments:

```yaml
# docker-compose.yml
version: '3.8'
services:
  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    ports:
      - "9090:9090"
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin
    volumes:
      - grafana_data:/var/lib/grafana

volumes:
  prometheus_data:
  grafana_data:
```

```yaml
# prometheus.yml - Scrape Docker stats
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'cadvisor'
    static_configs:
      - targets: ['localhost:8080']
```

**Datadog:**

SaaS monitoring platform with native Docker integration:

```bash
# Run Datadog agent
docker run -d --name datadog-agent \
  -e DD_API_KEY=<your-api-key> \
  -e DD_SITE=datadoghq.com \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  datadog/agent:latest
```

**Sysdig:**

Container security and monitoring focused on runtime behavior:

```bash
# Install Sysdig
docker run -it --rm \
  --volume /var/run/docker.sock:/host/var/run/docker.sock \
  --volume /proc:/host/proc:ro \
  sysdig/sysdig
```

**ELK Stack (Elasticsearch, Logstash, Kibana):**

Distributed logging platform for container log aggregation:

```yaml
# docker-compose.yml
version: '3.8'
services:
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.0.0
    environment:
      - xpack.security.enabled=false
      - discovery.type=single-node
    ports:
      - "9200:9200"

  kibana:
    image: docker.elastic.co/kibana/kibana:8.0.0
    ports:
      - "5601:5601"
    depends_on:
      - elasticsearch

  logstash:
    image: docker.elastic.co/logstash/logstash:8.0.0
    volumes:
      - ./logstash.conf:/usr/share/logstash/pipeline/logstash.conf
    depends_on:
      - elasticsearch
```

### Key Metrics to Monitor

Essential metrics for production container health:

**CPU Metrics:**
- `container_cpu_user_seconds_total`: User-space CPU time
- `container_cpu_system_seconds_total`: Kernel-space CPU time
- `container_cpu_usage_seconds_total`: Total CPU time
- CPU percentage as percentage of allocated limit
- Throttling events (container hitting CPU limit)

**Memory Metrics:**
- `container_memory_usage_bytes`: Total memory used
- `container_memory_max_usage_bytes`: Peak memory usage
- Memory percentage as percentage of limit
- Out-of-Memory (OOM) events
- Memory page faults (major and minor)

**Network I/O:**
- `container_network_receive_bytes_total`: Bytes received
- `container_network_transmit_bytes_total`: Bytes sent
- Packets received/transmitted
- Network errors and dropped packets

**Disk I/O:**
- `container_fs_read_bytes_total`: Bytes read from storage
- `container_fs_write_bytes_total`: Bytes written to storage
- I/O operations per second
- Disk usage and capacity percentage

**Container Lifecycle:**
- Container count (running, stopped, total)
- Container creation/deletion rate
- Container restart count and frequency
- Container uptime

**Health Status:**
- HEALTHCHECK success/failure rate
- Health check latency
- Number of unhealthy containers

Alert thresholds should be adjusted based on application requirements and expected behavior.

### Logging Best Practices

**Log to stdout/stderr:**

Containers should output logs to standard streams for Docker to capture:

```dockerfile
# Node.js application
FROM node:20-alpine
WORKDIR /app
COPY . .
RUN npm install
# Logs go to stdout
CMD ["node", "app.js"]
```

```javascript
// Log to stdout (not files)
console.log('Application started on port 3000');
console.error('Error occurred:', error);
```

**Docker Logging Drivers:**

Configure how Docker captures and stores container logs:

```bash
# View default logging driver
docker info | grep "Logging Driver"

# Set logging driver in run command
docker run --log-driver json-file myapp
docker run --log-driver syslog myapp
docker run --log-driver splunk myapp
```

Common logging drivers:

| Driver | Use Case |
|--------|----------|
| `json-file` | Default, stores logs in JSON format on host |
| `syslog` | Sends logs to syslog server |
| `fluentd` | Forwards to Fluentd for processing |
| `awslogs` | AWS CloudWatch Logs |
| `splunk` | Splunk HTTP Event Collector |
| `gcplogs` | Google Cloud Logging |

**Centralized Logging Configuration:**

```yaml
# daemon.json - Docker daemon default logging driver
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3",
    "labels": "service_name,service_version"
  }
}
```

```bash
# Per-container override
docker run \
  --log-driver fluentd \
  --log-opt fluentd-address=localhost:24224 \
  --log-opt tag=myapp \
  myapp
```

**Log Rotation:**

Prevent log files from consuming excessive disk space:

```json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3",
    "compress": "true"
  }
}
```

This keeps maximum 30 MB of logs per container (3 files × 10 MB).

## Production Best Practices

### CI/CD with Docker

**Build → Test → Push → Deploy Pipeline:**

Typical CI/CD workflow for Docker:

```yaml
# .github/workflows/docker-build.yml (GitHub Actions example)
name: Docker Build and Push

on:
  push:
    branches: [ main ]
    tags: [ 'v*' ]

jobs:
  build:
    runs-on: ubuntu-latest

    steps:
      # Checkout code
      - uses: actions/checkout@v3
        with:
          fetch-depth: 0  # For git SHA

      # Build image
      - name: Build Docker image
        run: |
          docker build -t myregistry.azurecr.io/myapp:${{ github.sha }} .
          docker build -t myregistry.azurecr.io/myapp:latest .

      # Scan image for vulnerabilities
      - name: Run Trivy vulnerability scanner
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: myregistry.azurecr.io/myapp:latest
          format: 'sarif'
          output: 'trivy-results.sarif'

      # Test image
      - name: Test Docker image
        run: |
          docker run --rm myregistry.azurecr.io/myapp:${{ github.sha }} npm test

      # Login to registry
      - name: Login to registry
        run: |
          echo "${{ secrets.REGISTRY_PASSWORD }}" | docker login \
            -u ${{ secrets.REGISTRY_USERNAME }} \
            --password-stdin myregistry.azurecr.io

      # Push image
      - name: Push to registry
        run: |
          docker push myregistry.azurecr.io/myapp:${{ github.sha }}
          docker push myregistry.azurecr.io/myapp:latest

      # Deploy (trigger deployment system)
      - name: Deploy
        run: |
          curl -X POST https://deploy.example.com/trigger \
            -H "Authorization: Bearer ${{ secrets.DEPLOY_TOKEN }}" \
            -d "image_tag=${{ github.sha }}"
```

**Image Tagging Strategy:**

Use semantic versioning combined with git SHA for traceability:

```bash
# Tag with version and git SHA
COMMIT_SHA=$(git rev-parse --short HEAD)
VERSION=$(git describe --tags --always)

docker build -t myapp:${VERSION}-${COMMIT_SHA} .
docker build -t myapp:${VERSION} .
docker build -t myapp:latest .

# Tag strategy: v1.2.3-abc1234 (version-commit)
# This allows rollback to specific build while tracking version history
```

### Image Registry Management

**Private Container Registries:**

Protect proprietary images using private registries:

| Registry | Platform | Features |
|----------|----------|----------|
| **Harbor** | Self-hosted | Vulnerability scanning, RBAC, image signing |
| **ECR** | AWS | Integrated with IAM, lifecycle policies |
| **ACR** | Azure | RBAC, webhook integration, image security |
| **GCR** | Google Cloud | Integration with Cloud Build, Cloud Run |
| **Quay** | Red Hat/Self-hosted | Security scanning, RBAC, CDN |

**Image Retention Policies:**

Prevent registry bloat and reduce storage costs:

```bash
# Example: Harbor lifecycle policy
# Keep last 10 images, delete images older than 30 days

# AWS ECR lifecycle policy
{
  "rules": [
    {
      "rulePriority": 1,
      "description": "Keep last 10 images",
      "selection": {
        "tagStatus": "any",
        "countType": "imageCountMoreThan",
        "countNumber": 10
      },
      "action": {
        "type": "expire"
      }
    },
    {
      "rulePriority": 2,
      "description": "Delete untagged images older than 30 days",
      "selection": {
        "tagStatus": "untagged",
        "countType": "sinceImagePushed",
        "countUnit": "days",
        "countNumber": 30
      },
      "action": {
        "type": "expire"
      }
    }
  ]
}
```

### Resource Management

**CPU Limits:**

Prevent single container from starving other containers:

```bash
# Limit to 2 CPUs
docker run --cpus=2 myapp

# Limit to 0.5 CPUs (50% of single CPU)
docker run --cpus=0.5 myapp

# CPU shares (relative priority, default 1024)
docker run --cpu-shares=512 myapp  # Half priority
```

**Memory Limits:**

Prevent OOM (Out-Of-Memory) killer from terminating containers unexpectedly:

```bash
# Hard limit: 512 MB
docker run --memory=512m myapp

# Soft limit + hard limit
docker run --memory=512m --memory-reservation=256m myapp

# Disable OOM killer (not recommended)
docker run --memory=512m --oom-kill-disable myapp

# Monitor memory usage
docker stats --no-stream myapp
```

**OOM Killer Behavior:**

```bash
# Default: kill container if it exceeds memory limit
docker run --memory=512m myapp

# Adjust OOM killer priority (-1000 to 1000, higher = more likely to kill)
docker run --memory=512m --oom-score-adj=500 myapp
```

### Health Checks

**HEALTHCHECK Instruction:**

Define how Docker determines if container is healthy:

```dockerfile
# Basic health check
FROM node:20-alpine
WORKDIR /app
COPY . .
RUN npm install
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:3000/health || exit 1
CMD ["npm", "start"]
```

Health check options:
- `--interval=30s`: Check every 30 seconds
- `--timeout=3s`: Fail if check takes > 3 seconds
- `--start-period=5s`: Grace period before first check
- `--retries=3`: Mark unhealthy after 3 failures

**Health Check in Docker Compose:**

```yaml
services:
  web:
    image: myapp:latest
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3000/health"]
      interval: 30s
      timeout: 3s
      retries: 3
      start_period: 5s
    ports:
      - "3000:3000"

  db:
    image: postgres:15
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 10s
      timeout: 5s
      retries: 5
```

**Monitor Health Status:**

```bash
# View container health status
docker ps --format "table {{.Names}}\t{{.Status}}"

# Output example:
# NAMES                 STATUS
# web                   Up 5 minutes (healthy)
# db                    Up 5 minutes (unhealthy)

# View health check logs
docker inspect --format='{{.State.Health}}' myapp
```

### Graceful Shutdown

**SIGTERM Handling:**

Containers receive SIGTERM signal during stop. Applications must handle this gracefully:

```python
# Python example - graceful shutdown
import signal
import sys
import time

def signal_handler(sig, frame):
    print('Shutting down gracefully...')
    # Close connections, flush buffers
    close_database_connections()
    flush_logs()
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# Application logic
while True:
    time.sleep(1)
```

```javascript
// Node.js example
process.on('SIGTERM', async () => {
  console.log('SIGTERM received, shutting down gracefully');

  // Stop accepting new requests
  server.close(() => {
    console.log('HTTP server closed');
  });

  // Close database connections
  await db.close();

  // Wait for existing requests to complete (with timeout)
  setTimeout(() => {
    console.log('Forced shutdown');
    process.exit(0);
  }, 30000);
});

server.listen(3000);
```

**Docker Compose Stop Grace Period:**

```yaml
services:
  web:
    image: myapp:latest
    stop_grace_period: 30s  # Allow 30s for graceful shutdown
    stop_signal: SIGTERM    # Send SIGTERM (default)
```

```bash
# Cli equivalent
docker stop --time=30 myapp
```

### Production Deployment Checklist

Comprehensive checklist for deploying containers to production:

**Security:**
- [ ] All images scanned for vulnerabilities
- [ ] No hardcoded secrets in Dockerfile or images
- [ ] Running as non-root user
- [ ] Linux capabilities dropped to minimum required
- [ ] Read-only root filesystem enabled
- [ ] Seccomp and AppArmor profiles applied
- [ ] Network policies restrict container communication
- [ ] TLS enabled for inter-service communication

**Resource Management:**
- [ ] CPU limits configured for all containers
- [ ] Memory limits configured with OOM prevention
- [ ] Request/limit ratios tested under load
- [ ] Disk space monitoring and cleanup policies in place

**Monitoring and Logging:**
- [ ] Application logs sent to stdout/stderr
- [ ] Centralized logging configured and tested
- [ ] Prometheus/monitoring agent running
- [ ] Alerting rules configured for key metrics
- [ ] Health checks configured and tested

**Networking:**
- [ ] Containers use user-defined networks
- [ ] Only required ports exposed
- [ ] Network policies restrict traffic
- [ ] Secrets not transmitted over unencrypted channels
- [ ] Service discovery configured properly

**High Availability:**
- [ ] Health checks properly implemented
- [ ] Container restart policies configured
- [ ] Multiple replicas running (if applicable)
- [ ] Graceful shutdown handling verified
- [ ] Database migrations handled properly

**Data and Storage:**
- [ ] Persistent data uses volumes/bind mounts
- [ ] Temporary data uses tmpfs where appropriate
- [ ] Backups configured and tested
- [ ] Data encryption at rest enabled

**Deployment and Rollback:**
- [ ] Blue-green or canary deployment strategy defined
- [ ] Rollback procedure documented and tested
- [ ] Database backward compatibility verified
- [ ] Load balancer/service mesh configuration correct
- [ ] DNS propagation time considered

**Documentation:**
- [ ] Dockerfile documented and optimized
- [ ] Deployment procedures documented
- [ ] Runbook for common issues created
- [ ] Disaster recovery plan established
- [ ] Team trained on deployment process

## Quick Reference

### Security Commands

```bash
# Run as non-root user
docker run --user 1000:1000 myapp

# Run as specific user and group
docker run --user appuser:appgroup myapp

# Read-only root filesystem
docker run --read-only myapp

# Read-only with tmpfs for temp files
docker run --read-only --tmpfs /tmp:size=100m myapp

# Drop all capabilities
docker run --cap-drop ALL myapp

# Drop all, add only NET_BIND_SERVICE
docker run --cap-drop ALL --cap-add NET_BIND_SERVICE myapp

# Apply seccomp profile
docker run --security-opt seccomp=default myapp

# Apply AppArmor profile
docker run --security-opt apparmor=docker-default myapp

# Limit resources
docker run --cpus=2 --memory=512m myapp

# Scan image for vulnerabilities
docker scout cves myimage:latest
trivy image myimage:latest
snyk container test myimage:latest

# Verify image digest
docker pull ubuntu@sha256:a7b8ffd33d65d7f0f2d2c0d2d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0

# Check container user
docker exec myapp whoami

# View health status
docker inspect --format='{{.State.Health.Status}}' myapp

# View container stats
docker stats myapp

# View security options
docker inspect --format='{{.HostConfig.SecurityOpt}}' myapp
```

### Exercises

**Exercise 1: Audit a Running Container for Security Issues**

Select a production container and perform a comprehensive security audit:

1. Identify base image and check for vulnerabilities
   ```bash
   docker inspect myapp --format='{{.Config.Image}}'
   trivy image <base-image>
   ```

2. Check running user
   ```bash
   docker exec myapp whoami
   docker exec myapp id
   ```

3. Verify Linux capabilities
   ```bash
   docker inspect --format='{{.HostConfig.CapAdd}}' myapp
   docker inspect --format='{{.HostConfig.CapDrop}}' myapp
   ```

4. Check resource limits
   ```bash
   docker inspect myapp --format='{{.HostConfig.Memory}}'
   docker inspect myapp --format='{{.HostConfig.CpuQuota}}'
   ```

5. Verify seccomp/AppArmor
   ```bash
   docker inspect --format='{{.HostConfig.SecurityOpt}}' myapp
   ```

6. Check open ports
   ```bash
   docker exec myapp netstat -tlnp
   ```

Document findings and create remediation plan.

**Exercise 2: Set Up Prometheus + Grafana to Monitor Docker Containers**

Create a monitoring stack for container metrics:

1. Create docker-compose.yml with Prometheus, Grafana, and cAdvisor
2. Configure Prometheus to scrape cAdvisor metrics
3. Deploy stack with `docker-compose up`
4. Access Grafana (port 3000) and create dashboard
5. Add data source pointing to Prometheus (localhost:9090)
6. Create graphs for:
   - CPU usage per container
   - Memory usage per container
   - Network I/O
   - Container count
7. Set up alerting rules for:
   - CPU > 80%
   - Memory > 90% of limit
   - Container restart count > 5
8. Generate load and verify metrics are collected
9. Document dashboard for team

**Exercise 3: Create a Secure Dockerfile Following Best Practices**

Build a hardened Node.js application image:

1. Use specific Node.js version, not latest
2. Use multi-stage build to exclude development dependencies
3. Create non-root user for running application
4. Implement `.dockerignore` file
5. Pin all npm dependency versions
6. Add HEALTHCHECK instruction
7. Use read-only filesystem with tmpfs for temporary files
8. Drop unnecessary capabilities
9. Scan final image with Trivy
10. Write documentation explaining security decisions

```dockerfile
# Reference template
FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production

FROM alpine:3.19
RUN apk add --no-cache dumb-init tini
RUN addgroup -g 1001 -S appuser && adduser -u 1001 -S appuser -G appuser
WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app/node_modules ./node_modules
COPY --chown=appuser:appuser . .
USER appuser
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD node healthcheck.js
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["node", "server.js"]
```

Document all security decisions and test thoroughly before deployment.
