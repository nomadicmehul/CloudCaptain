---
title: "Dockerfile Deep Dive"
sidebar_label: "Dockerfile Guide"
description: "Master Dockerfiles — instructions, multi-stage builds, best practices, and optimization techniques"
sidebar_position: 2
---

# Dockerfile Deep Dive

A Dockerfile is the blueprint for Docker images. This comprehensive guide covers everything from basic instructions to advanced optimization techniques.

## What is a Dockerfile?

A **Dockerfile** is a plain text file containing a series of instructions that Docker uses to automatically build an image. Instead of manually installing dependencies and configuring an environment, you write instructions that Docker executes layer by layer to create a reproducible, version-controlled image.

### Build Context

When you run `docker build`, Docker sends the **build context** (the directory and all its files) to the Docker daemon. The daemon reads the Dockerfile and executes each instruction.

```bash
# The current directory (.) is the build context
docker build -t myapp:1.0 .

# You can specify a different context directory
docker build -t myapp:1.0 /path/to/context

# Use -f to specify a Dockerfile outside the context
docker build -f /path/to/Dockerfile -t myapp:1.0 .
```

### How Docker Build Works

1. **Parse the Dockerfile** — Docker reads each instruction sequentially
2. **Create a temporary container** — For each instruction, Docker creates a container from the previous layer
3. **Execute the instruction** — Runs the command in the temporary container
4. **Create a new layer** — Saves the container's filesystem as a new layer
5. **Remove temporary container** — Cleans up the intermediate container
6. **Repeat** — Moves to the next instruction

This layer-based approach enables **caching** — if nothing changed in an instruction, Docker reuses the cached layer instead of rebuilding.

---

## Dockerfile Instructions Reference

Here's a comprehensive table of all Dockerfile instructions with descriptions and examples:

| Instruction | Purpose | Example |
|-------------|---------|---------|
| **FROM** | Sets the base image | `FROM ubuntu:22.04` |
| **LABEL** | Adds metadata to image | `LABEL version="1.0" maintainer="dev@example.com"` |
| **RUN** | Executes commands during build | `RUN apt-get update && apt-get install -y curl` |
| **CMD** | Default command when container starts | `CMD ["npm", "start"]` |
| **ENTRYPOINT** | Configures container as executable | `ENTRYPOINT ["python", "app.py"]` |
| **COPY** | Copies files from context to image | `COPY ./app /app` |
| **ADD** | Copies files + auto-extracts archives | `ADD ./app.tar.gz /app` |
| **ENV** | Sets environment variables | `ENV NODE_ENV=production` |
| **ARG** | Build-time variables | `ARG BUILD_DATE` |
| **WORKDIR** | Sets working directory | `WORKDIR /app` |
| **EXPOSE** | Documents exposed ports (metadata) | `EXPOSE 8080` |
| **VOLUME** | Creates mount points | `VOLUME ["/data", "/logs"]` |
| **USER** | Sets the user/UID to run commands | `USER appuser:appgroup` |
| **HEALTHCHECK** | Defines health probe | `HEALTHCHECK --interval=30s CMD curl localhost:8080` |
| **ONBUILD** | Triggers in child images | `ONBUILD RUN npm install` |
| **STOPSIGNAL** | Sets signal for container stop | `STOPSIGNAL SIGTERM` |
| **SHELL** | Sets default shell | `SHELL ["/bin/bash", "-c"]` |

### Detailed Instruction Examples

#### FROM
The first instruction in any Dockerfile. Specifies the base image (OS + runtime).

```dockerfile
FROM ubuntu:22.04
FROM python:3.11-slim
FROM node:20-alpine
FROM scratch  # Empty image, for static binaries only
```

#### LABEL
Adds metadata (name-value pairs) to your image. Useful for documentation and automation.

```dockerfile
LABEL version="1.0.0"
LABEL maintainer="devops@company.com"
LABEL description="Production API server"
LABEL environment="prod"
```

View labels with: `docker image inspect myapp:1.0 --format='{{json .Config.Labels}}'`

#### RUN
Executes shell commands during image build. Each RUN creates a new layer.

```dockerfile
# Shell form (runs in /bin/sh -c)
RUN apt-get update && apt-get install -y curl

# Exec form (no shell, better for signals)
RUN ["apt-get", "install", "-y", "curl"]

# Multiple commands in one RUN (single layer)
RUN apt-get update && \
    apt-get install -y curl wget git && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
```

**Best practice**: Combine multiple RUN instructions to reduce layers and image size.

#### CMD
Provides the default command to run when a container starts. Can be overridden.

```dockerfile
# Exec form (preferred - no shell interpretation)
CMD ["python", "app.py"]

# Shell form (runs in shell)
CMD python app.py

# As default parameters to ENTRYPOINT
CMD ["--port", "8080"]
```

Run a different command: `docker run myapp python test.py`

#### ENTRYPOINT
Configures the container to run as an executable. Harder to override than CMD.

```dockerfile
# Exec form (preferred)
ENTRYPOINT ["python", "app.py"]

# Shell form
ENTRYPOINT python app.py
```

When ENTRYPOINT and CMD both exist, CMD becomes default arguments:

```dockerfile
ENTRYPOINT ["python"]
CMD ["app.py"]
# Results in: python app.py
# Can override with: docker run myapp config.py  →  python config.py
```

#### COPY
Copies files/directories from build context to image.

```dockerfile
# Copy single file
COPY requirements.txt /app/

# Copy directory
COPY ./src /app/src

# Copy with ownership
COPY --chown=appuser:appgroup ./app /app

# Multiple sources
COPY --chown=appuser:appgroup file1.txt file2.txt /app/
```

#### ADD
Like COPY, but also auto-extracts .tar files and supports URLs. **Avoid unless you need extraction.**

```dockerfile
# Extract tar archive
ADD ./app.tar.gz /app

# Download from URL (not recommended — use RUN curl instead)
ADD https://example.com/file.tar.gz /app
```

**Why prefer COPY**: It's explicit and more predictable. Use `RUN tar -xzf` if you need extraction.

#### ENV
Sets environment variables in the image. Available to all subsequent instructions and running containers.

```dockerfile
ENV NODE_ENV=production
ENV PORT=8080
ENV DEBUG=false

# Multiple variables
ENV APP_VERSION=2.0 \
    APP_USER=appuser \
    LOG_LEVEL=info
```

Access in containers: `echo $NODE_ENV`

#### ARG
Build-time variables. Only available during build, not in running containers.

```dockerfile
ARG BUILD_DATE
ARG VERSION=1.0.0
ARG BASE_IMAGE=ubuntu:22.04

FROM ${BASE_IMAGE}
RUN echo "Built on ${BUILD_DATE}"

LABEL version="${VERSION}"
```

Pass at build time: `docker build --build-arg VERSION=2.0.1 .`

#### WORKDIR
Sets the working directory for RUN, CMD, ENTRYPOINT, COPY, ADD.

```dockerfile
WORKDIR /app
COPY . .  # Copies to /app
RUN npm install  # Runs in /app

WORKDIR /app/src
RUN ls  # Lists /app/src
```

Creates the directory if it doesn't exist.

#### EXPOSE
Documents which ports the application listens on. **Doesn't publish ports** — just metadata.

```dockerfile
EXPOSE 8080
EXPOSE 3000 5000
EXPOSE 9000/tcp 9000/udp
```

Actual publishing requires `docker run -p 8080:8080`

#### VOLUME
Declares mount points for persistent data or sharing. Creates an anonymous volume.

```dockerfile
VOLUME ["/data", "/logs"]

# Or individual
VOLUME /data
VOLUME /logs
```

Data in volumes persists even if container is deleted. Better to use named volumes: `docker run -v mydata:/data`

#### USER
Specifies which user runs subsequent instructions and the container.

```dockerfile
USER appuser
USER 1000  # By UID
USER appuser:appgroup  # With group
```

**Security best practice**: Run as non-root user instead of root.

#### HEALTHCHECK
Defines a command Docker runs to check container health.

```dockerfile
# Check if web server responds
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD curl -f http://localhost:8080/health || exit 1

# With start period (grace period before first check)
HEALTHCHECK --start-period=5s --interval=10s \
  CMD ["python", "health_check.py"]
```

View health status: `docker ps` (shows `healthy`, `unhealthy`, or `starting`)

#### ONBUILD
Triggers instructions in child images that use this image as base.

```dockerfile
# In base-image Dockerfile
ONBUILD COPY . /app
ONBUILD RUN npm install

# In child-image Dockerfile
FROM base-image  # Triggers the ONBUILD instructions automatically
```

Used rarely, mainly for base images meant to be extended.

#### STOPSIGNAL
Specifies the signal sent when stopping a container.

```dockerfile
STOPSIGNAL SIGTERM  # Default
STOPSIGNAL SIGKILL  # Immediate kill
```

#### SHELL
Sets the default shell for RUN, CMD, ENTRYPOINT (shell form).

```dockerfile
# Use bash instead of sh (alpine doesn't have bash by default)
RUN apk add --no-cache bash
SHELL ["/bin/bash", "-c"]

RUN echo "Using bash now"
```

---

## CMD vs ENTRYPOINT

This is one of the most confusing topics in Docker. Let's clarify:

### Shell Form vs Exec Form

**Shell Form** (runs in a shell):
```dockerfile
CMD python app.py
ENTRYPOINT python app.py
```

**Exec Form** (direct execution, no shell):
```dockerfile
CMD ["python", "app.py"]
ENTRYPOINT ["python", "app.py"]
```

**Key difference**: Shell form allows shell features (pipes, redirects), but exec form is preferred because signals are handled correctly.

### When CMD is Overridden

```dockerfile
# Dockerfile
FROM ubuntu:22.04
CMD ["echo", "Hello from CMD"]

# Run normally
docker run myapp
# Output: Hello from CMD

# Override CMD
docker run myapp echo "Override!"
# Output: Override!
```

### When ENTRYPOINT Prevents Override

```dockerfile
# Dockerfile
FROM ubuntu:22.04
ENTRYPOINT ["echo", "Hello from ENTRYPOINT"]

# Run normally
docker run myapp
# Output: Hello from ENTRYPOINT

# Try to override (this just adds arguments!)
docker run myapp "some text"
# Output: Hello from ENTRYPOINT some text
```

To override ENTRYPOINT, use: `docker run --entrypoint /bin/bash myapp`

### Using Both Together

```dockerfile
ENTRYPOINT ["python"]
CMD ["app.py"]
```

- `docker run myapp` → `python app.py`
- `docker run myapp config.py` → `python config.py` (CMD is replaced)
- `docker run --entrypoint /bin/bash myapp` → `/bin/bash` (ENTRYPOINT is replaced)

### Best Practices

- **Use ENTRYPOINT** when the container is an executable service (API, worker)
- **Use CMD** when you want easy argument override
- **Combine both** when you have a fixed command with optional parameters

**Example — Flask API**:
```dockerfile
ENTRYPOINT ["python"]
CMD ["app.py"]
```

- Normal: `python app.py`
- With argument: `docker run myapp app.py --debug`

---

## COPY vs ADD

Both copy files from build context to image, but with important differences:

### COPY — Preferred

```dockerfile
COPY ./requirements.txt /app/
COPY --chown=appuser:appgroup ./src /app/src
```

**Advantages**:
- Explicit and predictable
- No auto-extraction surprises
- Better caching behavior
- Recommended by Docker

### ADD — More Features

```dockerfile
# Auto-extracts tar archives
ADD ./app.tar.gz /app

# Downloads from URLs (not recommended)
ADD https://example.com/app.tar.gz /app
```

**Disadvantages**:
- Implicit behavior (hidden magic)
- Slower (needs to check if file is tar)
- Auto-extraction can be surprising
- Remote URLs are unreliable

### Comparison Table

| Feature | COPY | ADD |
|---------|------|-----|
| Copy files | ✓ | ✓ |
| Auto-extract tar | ✗ | ✓ |
| Download from URL | ✗ | ✓ |
| Cache-friendly | ✓ | ✗ |
| Predictable | ✓ | ✗ |
| Recommended | ✓ | ✗ |

### When to Use ADD

Only for tar extraction:

```dockerfile
# OK — extracting application archive
ADD ./app-v1.2.3.tar.gz /app

# Better — be explicit
RUN tar -xzf /tmp/app.tar.gz -C /app && rm /tmp/app.tar.gz
```

**Never use ADD for URLs**. Download with RUN instead:

```dockerfile
# Bad
ADD https://example.com/tool.tar.gz /app

# Good
RUN curl -L https://example.com/tool.tar.gz -o /tmp/tool.tar.gz && \
    tar -xzf /tmp/tool.tar.gz -C /app && \
    rm /tmp/tool.tar.gz
```

---

## Multi-Stage Builds

Multi-stage builds dramatically reduce image size by separating **build stage** from **production stage**.

### The Problem

```dockerfile
# Single stage — large image!
FROM node:20

WORKDIR /app
COPY package.json .
RUN npm install  # Dependencies bloat the image

COPY . .
RUN npm run build  # Build tools included

EXPOSE 3000
CMD ["npm", "start"]
```

Final image includes: Node.js, npm, build tools, and dependencies — size: ~900MB.

### The Solution: Multi-Stage

```dockerfile
# Stage 1: Build
FROM node:20 AS builder

WORKDIR /app
COPY package.json .
RUN npm ci --omit=dev

COPY . .
RUN npm run build

# Stage 2: Production
FROM node:20-alpine

WORKDIR /app
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/dist ./dist
COPY package.json .

EXPOSE 3000
CMD ["node", "dist/index.js"]
```

Final image: ~150MB (6x smaller!) — only includes runtime dependencies.

### Example: Node.js Application

```dockerfile
# Builder stage
FROM node:20 AS builder

WORKDIR /app
COPY package*.json ./
RUN npm ci

COPY . .
RUN npm run build

# Production stage
FROM node:20-alpine

WORKDIR /app

# Only copy production dependencies
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/dist ./dist
COPY package.json .

ENV NODE_ENV=production
EXPOSE 3000

HEALTHCHECK --interval=30s CMD curl localhost:3000/health || exit 1

USER node
CMD ["node", "dist/index.js"]
```

### Example: Go Application

```dockerfile
# Build stage
FROM golang:1.21 AS builder

WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o app .

# Production stage — minimal scratch image
FROM scratch

COPY --from=builder /build/app /app

EXPOSE 8080
ENTRYPOINT ["/app"]
```

Final size: ~15MB! (vs 1.2GB with full golang base image)

### Example: Python Application

```dockerfile
# Build stage
FROM python:3.11 AS builder

WORKDIR /build
COPY requirements.txt .

RUN pip install --user --no-cache-dir -r requirements.txt

# Production stage
FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /root/.local /root/.local
COPY ./app .

ENV PATH=/root/.local/bin:$PATH

EXPOSE 5000
CMD ["python", "app.py"]
```

Reduces size from 900MB to 200MB.

### Multi-Stage Best Practices

1. **Name your stages** — `AS builder`, `AS runner`
2. **Copy only what you need** — `--from=builder`
3. **Use appropriate base images** — alpine for production, full image for build
4. **Separate concerns** — one stage per responsibility
5. **Consider build cache** — order layers for efficiency

---

## Dockerfile Best Practices

### 1. Use Specific Base Image Tags

❌ **Bad** — Image breaks when maintainer updates:
```dockerfile
FROM ubuntu
FROM node
FROM python
```

✓ **Good** — Pinned, reproducible:
```dockerfile
FROM ubuntu:22.04
FROM node:20.10-alpine
FROM python:3.11-slim
```

### 2. Minimize Layers (Combine RUN Commands)

❌ **Bad** — Each RUN creates a layer:
```dockerfile
RUN apt-get update
RUN apt-get install -y curl
RUN apt-get install -y wget
RUN apt-get install -y git
RUN apt-get clean
```

Image has 4 layers, wasted space.

✓ **Good** — Single layer:
```dockerfile
RUN apt-get update && \
    apt-get install -y curl wget git && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
```

### 3. Use .dockerignore

❌ **Bad** — Large build context:
```
# No .dockerignore
# Everything gets sent to daemon!
```

✓ **Good** — Exclude unnecessary files:
```dockerfile
# .dockerignore
node_modules/
npm-debug.log
.git
.gitignore
.DS_Store
.env
.env.local
dist/
build/
*.log
.vscode/
.idea/
coverage/
test/
```

### 4. Run as Non-Root User

❌ **Bad** — Container runs as root:
```dockerfile
FROM ubuntu:22.04
RUN apt-get update && apt-get install -y curl
CMD ["python", "app.py"]
```

✓ **Good** — Create unprivileged user:
```dockerfile
FROM ubuntu:22.04
RUN apt-get update && apt-get install -y curl && \
    useradd -m -s /sbin/nologin appuser

USER appuser
CMD ["python", "app.py"]
```

### 5. Use COPY Instead of ADD

✓ **Good** — Explicit:
```dockerfile
COPY ./requirements.txt /app/
COPY ./src /app/src
```

❌ **Bad** — Implicit behavior:
```dockerfile
ADD ./app.tar.gz /app  # Auto-extracts!
ADD https://example.com/file.tar.gz /app  # Remote, unreliable
```

### 6. Set HEALTHCHECK

✓ **Good** — Docker monitors container health:
```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s \
  CMD curl -f http://localhost:8080/health || exit 1
```

### 7. Order Instructions for Cache Optimization

✓ **Good** — Dependencies before code:
```dockerfile
COPY package.json .
RUN npm install  # Cached unless package.json changes

COPY . .  # Code changes frequently
RUN npm run build
```

❌ **Bad** — Code before dependencies:
```dockerfile
COPY . .  # Changes frequently, invalidates cache
RUN npm install  # Rebuilt every time!
```

### 8. Clean Up in Same Layer

❌ **Bad** — Cache persists in separate layer:
```dockerfile
RUN apt-get update
RUN apt-get install -y curl wget
RUN apt-get clean  # Doesn't reduce image!
```

✓ **Good** — Clean in same RUN:
```dockerfile
RUN apt-get update && \
    apt-get install -y curl wget && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
```

### 9. Use Multi-Stage Builds

✓ **Good** — Separate build and runtime:
```dockerfile
FROM node:20 AS builder
RUN npm install  # 900MB

FROM node:20-alpine
COPY --from=builder /app /app  # Only 150MB
```

### 10. Pin Dependency Versions

❌ **Bad** — Unpredictable versions:
```dockerfile
RUN pip install flask django requests
RUN npm install express
```

✓ **Good** — Fixed versions:
```dockerfile
RUN pip install flask==2.3.0 django==4.2.0 requests==2.31.0
RUN npm install express@4.18.2
```

Or use lock files:
```dockerfile
COPY requirements.txt constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt

COPY package-lock.json ./
RUN npm ci
```

### 11. Use Official Base Images

✓ **Good** — Maintained, documented:
```dockerfile
FROM ubuntu:22.04
FROM python:3.11-slim
FROM node:20-alpine
FROM golang:1.21
```

❌ **Bad** — Unmaintained, unknown origin:
```dockerfile
FROM some-random-user/ubuntu
FROM sketchy-base:latest
```

### 12. Set Meaningful Labels/Metadata

✓ **Good** — Document your image:
```dockerfile
LABEL maintainer="devops@company.com"
LABEL version="1.0.0"
LABEL description="Production API server"
LABEL environment="prod"
LABEL org.opencontainers.image.created="2024-01-15"
LABEL org.opencontainers.image.url="https://github.com/company/repo"
```

Query labels: `docker inspect myapp:1.0 --format='{{json .Config.Labels}}'`

---

## Image Size Optimization

Reducing image size improves:
- **Faster deployment** — Smaller downloads
- **Lower storage costs** — Less disk space
- **Better security** — Fewer vulnerabilities

### Technique 1: Alpine Base Images

```dockerfile
# Full Ubuntu: 80MB
FROM ubuntu:22.04

# Alpine Linux: 7MB
FROM alpine:3.18

# Node slim: 180MB
FROM node:20
# Node alpine: 50MB
FROM node:20-alpine

# Python slim: 150MB
FROM python:3.11
# Python alpine: 55MB
FROM python:3.11-alpine
```

Alpine is tiny but may have slight compatibility issues. Test before using in production.

### Technique 2: Multi-Stage Builds

```dockerfile
# Bad — 900MB
FROM node:20
COPY . .
RUN npm install
CMD ["npm", "start"]

# Good — 150MB
FROM node:20 AS builder
COPY . .
RUN npm install

FROM node:20-alpine
COPY --from=builder /app /app
CMD ["npm", "start"]
```

### Technique 3: Remove Package Manager Caches

```dockerfile
# Ubuntu/Debian
RUN apt-get update && \
    apt-get install -y curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Alpine
RUN apk add --no-cache curl

# Python
RUN pip install --no-cache-dir flask
```

### Technique 4: Aggressive Multi-Stage

```dockerfile
FROM golang:1.21 AS builder
WORKDIR /build
COPY . .
RUN CGO_ENABLED=0 go build -o app -ldflags="-s -w" .

FROM scratch  # Empty!
COPY --from=builder /build/app /
ENTRYPOINT ["/app"]
```

Size: 10MB (compiled Go binary + nothing else)

### Technique 5: .dockerignore

```
# .dockerignore
node_modules/
npm-debug.log
.git
.gitignore
.env
.DS_Store
dist/
build/
*.log
.vscode/
.idea/
test/
README.md
LICENSE
```

Exclude large files from build context.

---

## .dockerignore

Like `.gitignore`, but for Docker build context. Prevents unnecessary files from being sent to the daemon.

### Syntax

```dockerfile
# Comments are allowed
# Wildcard patterns

*.log              # Ignore all .log files
test/              # Ignore test directory
.env               # Ignore .env file
node_modules/**    # Ignore everything in node_modules
!important.log     # Negation — don't ignore important.log
```

### Common Patterns

```dockerfile
# .dockerignore

# Version control
.git
.gitignore
.gitlab-ci.yml
.github/

# Dependencies (will be installed in image)
node_modules/
pip-cache/
vendor/

# Build artifacts
build/
dist/
*.o
*.out

# Logs
*.log
npm-debug.log
yarn-error.log

# Environment files
.env
.env.local
.env.*.local

# IDE
.vscode/
.idea/
*.swp
*.swo
.DS_Store

# Documentation (usually not needed in image)
README.md
CONTRIBUTING.md
LICENSE

# Test files
test/
tests/
__tests__/
coverage/
.coverage
```

### Example .dockerignore

```dockerfile
# .dockerignore for Node.js app
.git
.gitignore
node_modules
npm-debug.log
.env
.env.local
.vscode
.idea
dist
test
coverage
README.md
.DS_Store
```

---

## Build Cache

Docker uses **layer caching** to speed up builds. If an instruction hasn't changed, Docker reuses the cached layer.

### How Caching Works

```dockerfile
# Layer 1: FROM ubuntu:22.04 ← Cached
# Layer 2: RUN apt-get update ← Cached (same instruction)
# Layer 3: COPY requirements.txt . ← Cached (if requirements.txt unchanged)
# Layer 4: RUN pip install ← Invalidated if Layer 3 changed
# Layer 5: COPY . . ← Invalidated (code changed)
# Layer 6: RUN python app.py ← Rebuilt
```

### Cache Invalidation

Once a layer is invalidated, all subsequent layers rebuild:

```dockerfile
COPY . .  # Changes frequently → invalidates cache
RUN pip install -r requirements.txt  # Rebuild even if requirements.txt unchanged!
```

### Optimization: Order Matters

✓ **Good** — Dependencies before code:
```dockerfile
FROM python:3.11
WORKDIR /app

COPY requirements.txt .  # Stable
RUN pip install -r requirements.txt  # Cached until requirements.txt changes

COPY . .  # Changes frequently
RUN python app.py
```

❌ **Bad** — Code before dependencies:
```dockerfile
FROM python:3.11
WORKDIR /app

COPY . .  # Changes frequently
RUN pip install -r requirements.txt  # Always rebuilt!
```

### Disable Cache

```bash
# Force rebuild from scratch
docker build --no-cache .

# Disable cache for specific instruction
docker build --build-arg CACHE_BUST=$(date +%s) .
```

### Cache from Remote Registry

```bash
# Pull cached layers from registry first
docker build --cache-from myregistry/myapp:latest .
```

Used in CI/CD for faster builds.

---

## Practical Examples

### Example 1: Node.js Web Application

A production-ready Node.js API with multi-stage build:

```dockerfile
# ============ BUILDER STAGE ============
FROM node:20 AS builder

WORKDIR /build

# Copy dependency files
COPY package*.json ./

# Install dependencies
RUN npm ci

# Copy source code
COPY . .

# Build application
RUN npm run build

# ============ PRODUCTION STAGE ============
FROM node:20-alpine

WORKDIR /app

# Install dumb-init (safer signal handling)
RUN apk add --no-cache dumb-init

# Create non-root user
RUN addgroup -S nodejs && adduser -S nodejs -G nodejs

# Copy built application
COPY --from=builder --chown=nodejs:nodejs /build/dist ./dist
COPY --from=builder --chown=nodejs:nodejs /build/node_modules ./node_modules
COPY --from=builder --chown=nodejs:nodejs /build/package.json .

# Set metadata
LABEL maintainer="devops@company.com"
LABEL version="1.0.0"

# Environment
ENV NODE_ENV=production
ENV PORT=3000

# Switch to non-root user
USER nodejs

# Expose port
EXPOSE 3000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s \
  CMD node -e "require('http').get('http://localhost:3000/health', (r) => {if (r.statusCode !== 200) throw new Error(r.statusCode)})"

# Use dumb-init to handle signals properly
ENTRYPOINT ["dumb-init", "--"]
CMD ["node", "dist/index.js"]
```

### Example 2: Python Flask API

```dockerfile
# ============ BUILDER STAGE ============
FROM python:3.11 AS builder

WORKDIR /build

# Copy requirements
COPY requirements.txt .

# Install dependencies to /build/.local
RUN pip install --user --no-cache-dir -r requirements.txt

# ============ PRODUCTION STAGE ============
FROM python:3.11-slim

WORKDIR /app

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Copy Python dependencies from builder
COPY --from=builder /build/.local /home/appuser/.local
COPY --from=builder /build/.local/bin /home/appuser/.local/bin

# Copy application
COPY --chown=appuser:appuser . .

# Set Python path
ENV PATH=/home/appuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=app.py \
    FLASK_ENV=production

EXPOSE 5000

USER appuser

HEALTHCHECK --interval=30s --timeout=10s \
  CMD curl -f http://localhost:5000/health || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "wsgi:app"]
```

### Example 3: Java Spring Boot Application

```dockerfile
# ============ BUILDER STAGE ============
FROM maven:3.9-eclipse-temurin-21 AS builder

WORKDIR /build

COPY pom.xml .

# Download dependencies
RUN mvn dependency:go-offline -B

COPY src ./src

# Build application
RUN mvn clean package -DskipTests

# ============ PRODUCTION STAGE ============
FROM eclipse-temurin:21-jre

WORKDIR /app

# Create non-root user
RUN groupadd -r spring && useradd -r -g spring spring

# Copy JAR from builder
COPY --from=builder --chown=spring:spring /build/target/*.jar app.jar

ENV JAVA_OPTS="-Xmx256m -Xms64m"

EXPOSE 8080

USER spring

HEALTHCHECK --interval=30s --timeout=10s \
  CMD curl -f http://localhost:8080/actuator/health || exit 1

CMD ["java", "-jar", "app.jar"]
```

### Example 4: Static Site with Nginx

```dockerfile
# ============ BUILDER STAGE ============
FROM node:20 AS builder

WORKDIR /build

COPY package*.json ./
RUN npm ci

COPY . .

# Build static site
RUN npm run build

# ============ PRODUCTION STAGE ============
FROM nginx:alpine

# Copy Nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy built assets
COPY --from=builder /build/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=10s \
  CMD wget --quiet --tries=1 --spider http://localhost/index.html || exit 1

CMD ["nginx", "-g", "daemon off;"]
```

---

## Exercises

### Exercise 1: Write a Dockerfile for a Flask App

Create a Dockerfile for this simple Flask application:

**app.py**:
```python
from flask import Flask

app = Flask(__name__)

@app.route('/')
def hello():
    return 'Hello from Flask!'

@app.route('/health')
def health():
    return {'status': 'ok'}, 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
```

**requirements.txt**:
```
Flask==2.3.0
gunicorn==21.0.0
```

**Requirements**:
- Use official Python base image
- Install dependencies from requirements.txt
- Set working directory to /app
- Expose port 5000
- Run as non-root user
- Include health check
- Use multi-stage build if possible

<details>
<summary>Solution</summary>

```dockerfile
FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.11-slim

WORKDIR /app

RUN groupadd -r appuser && useradd -r -g appuser appuser

COPY --from=builder /root/.local /root/.local
COPY app.py .

ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1

EXPOSE 5000

USER appuser

HEALTHCHECK --interval=30s CMD curl -f http://localhost:5000/health || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
```

</details>

### Exercise 2: Optimize a "Bad" Dockerfile

Here's a poorly written Dockerfile. Identify and fix at least 5 issues:

```dockerfile
FROM ubuntu:latest

RUN apt-get update
RUN apt-get install -y curl
RUN apt-get install -y git
RUN apt-get install -y python3
RUN apt-get install -y python3-pip

WORKDIR /src

COPY . .

RUN pip install flask django

ENV PORT 5000
ENV DEBUG true

CMD python3 app.py
```

**Issues to find**:
1. Uses `latest` tag
2. Multiple RUN commands (wasted layers)
3. Missing `apt-get clean`
4. Runs as root
5. No HEALTHCHECK
6. No .dockerignore equivalent mentioned
7. Missing EXPOSE

<details>
<summary>Solution</summary>

```dockerfile
FROM ubuntu:22.04

RUN apt-get update && \
    apt-get install -y curl git python3 python3-pip && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    useradd -m -s /sbin/nologin appuser

WORKDIR /app

COPY --chown=appuser:appuser . .

RUN pip install --no-cache-dir flask==2.3.0 django==4.2.0

ENV PORT=5000 \
    DEBUG=false \
    PYTHONUNBUFFERED=1

EXPOSE 5000

USER appuser

HEALTHCHECK --interval=30s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/')" || exit 1

CMD ["python3", "app.py"]
```

Plus create **.dockerignore**:
```
__pycache__
*.pyc
.env
.git
test/
.vscode
README.md
```

</details>

### Exercise 3: Create a Multi-Stage Build for Go

Write a production Dockerfile for a Go application that:
- Compiles the Go binary in a build stage
- Uses `scratch` (empty) image for production
- Minimizes final image size
- Includes metadata labels

**Source**: Assume `main.go` that builds to binary `myapp`

**Requirements**:
- Compile with `CGO_ENABLED=0` (no C dependencies)
- Use `-ldflags="-s -w"` to strip binary
- Copy only the compiled binary to final stage
- Add meaningful labels
- Set ENTRYPOINT to run the binary

<details>
<summary>Solution</summary>

```dockerfile
# ============ BUILD STAGE ============
FROM golang:1.21 AS builder

WORKDIR /build

# Copy go mod files
COPY go.mod go.sum ./

# Download dependencies
RUN go mod download

# Copy source
COPY . .

# Build statically-linked binary
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -ldflags="-s -w" -o myapp .

# ============ PRODUCTION STAGE ============
FROM scratch

# Add ca-certificates for HTTPS (if needed)
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/

# Copy binary
COPY --from=builder /build/myapp /

# Metadata
LABEL maintainer="devops@company.com"
LABEL version="1.0.0"
LABEL description="Minimal Go application"

EXPOSE 8080

ENTRYPOINT ["/myapp"]
```

**Final size**: ~15MB (vs 1.2GB with full golang image)

</details>

---

## Summary

Dockerfiles are powerful tools for building reproducible, portable containers. Master these key concepts:

- **Instructions** — FROM, RUN, COPY, ENTRYPOINT, etc.
- **Layering** — Each instruction creates a layer; optimize layer count
- **Caching** — Order instructions for maximum cache hits
- **Multi-stage builds** — Separate build from production for smaller images
- **Best practices** — Non-root users, pinned versions, minimal layers
- **Optimization** — Alpine images, .dockerignore, cache cleanup

The investment in learning Dockerfile best practices pays dividends in faster deployments, smaller images, and more secure containers.
