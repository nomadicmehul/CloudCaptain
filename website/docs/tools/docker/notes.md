---
title: "Docker Notes"
description: "In-depth Docker concepts and learning notes"
---

# Docker Deep Dive Notes

## Container vs VM

| Feature | Container | Virtual Machine |
|:--------|:----------|:----------------|
| Isolation | Process-level | Hardware-level |
| Boot time | Seconds | Minutes |
| Size | MBs | GBs |
| Performance | Near-native | Overhead from hypervisor |
| OS | Shares host kernel | Full OS per VM |

## Dockerfile Best Practices

1. **Use multi-stage builds** to reduce image size
2. **Use specific base image tags** (not `latest`)
3. **Minimize layers** — combine RUN commands
4. **Use .dockerignore** to exclude unnecessary files
5. **Run as non-root user** for security
6. **Use COPY instead of ADD** unless you need tar extraction
7. **Set health checks** with HEALTHCHECK instruction

## Multi-Stage Build Example

```dockerfile
# Build stage
FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production
COPY . .
RUN npm run build

# Production stage
FROM node:20-alpine
WORKDIR /app
COPY --from=builder /app/dist ./dist
COPY --from=builder /app/node_modules ./node_modules
USER node
EXPOSE 3000
CMD ["node", "dist/index.js"]
```

## Docker Security Checklist

- Use official or verified base images
- Scan images for vulnerabilities (Trivy, Snyk)
- Don't run containers as root
- Use read-only file systems where possible
- Limit container resources (CPU, memory)
- Don't store secrets in images
- Keep images up to date

## Docker Architecture

### Components

- **Docker Client** — The UI/interface that users interact with. Communicates with the Docker Daemon via CLI commands
- **Docker Daemon** — Runs on the host machine and executes commands from the Docker Client (building, running, distributing containers)
- **REST API** — Remote interface for interacting with the Docker Daemon

### Runtime Stack

```
Docker Client → Docker Daemon → Container Runtime
                              → Images
                              → Volumes
                              → Networks
```

## Images vs Containers

### Docker Image

- A **blueprint** or **template** for creating containers
- Read-only collection of layers stacked on top of each other
- Each image references a list of read-only layers representing filesystem differences
- Contains everything needed to run an application:
  - Code
  - Runtime
  - Libraries
  - Environment variables
  - Configuration files
- Think of it as an archive or "jar file" that can be deployed anywhere
- Multiple containers can be created from the same image

### Docker Container

- A **running instance** of a Docker image
- Standard unit of software that packages code and all dependencies
- Lightweight and standalone executable package
- Has one **writable top layer** on top of the image layers (container layer)
- When you delete a container, the writable layer is deleted
- Shares the host machine's OS kernel (no need for separate OS per container)
- Changes made to a running container are written to the container layer

## Why Docker?

### The Problem Docker Solves

In traditional development:
- Application works on developer's machine but fails in testing/production
- Different versions of software across environments (dev vs test vs prod)
- Resource waste with virtual machines (each service needs a full OS)

### Docker Solution

- **Containerization** — Package application with all dependencies (libraries, runtime, environment)
- **Consistency** — Same container runs identically in dev, testing, and production
- **Efficiency** — Containers share the host OS kernel, no per-app OS overhead
- **Lightweight** — Containers use minimal resources compared to VMs

## Microservices Architecture with Docker

### Concept

Breaking down monolithic applications into smaller, independently deployable services.

### Example: E-commerce Application

Instead of one large application, create separate services:
- Account Service
- Product Catalog Service
- Shopping Cart Service
- Order Processing Service

### Advantages

- **Easier to build and maintain** — Each service is smaller and focused
- **Simpler updates** — Changes to one service don't affect others; fewer dependencies
- **Resilience** — If one service fails, others continue functioning
- **Scalability** — Scale individual services based on demand
- **Technology flexibility** — Each service can use different technologies

## Volumes

### What are Volumes?

- Initialized when a container is created
- Allow you to persist and share container data
- Exist as normal directories/files on the host filesystem
- Separate from the container's default Union File System
- Persist even if the container is destroyed, updated, or rebuilt

### Key Benefits

- **Data persistence** across container lifecycle
- **Data sharing** between multiple containers
- **Decoupling** of data from container

## Docker Engine Components

### Three Core Parts

1. **Docker Daemon** — The background service managing containers and images
2. **Docker Client** — Command-line interface for users to interact with Docker
3. **REST API** — Programmatic interface for interacting with the Docker Daemon

## Dockerfile Reference

### Common Instructions

```dockerfile
# Start with a base image
FROM ubuntu:latest

# Set metadata
LABEL maintainer="your.email@example.com"

# Run commands during build
RUN apt-get update && apt-get install -y python3

# Set working directory
WORKDIR /app

# Copy files from host to container
COPY requirements.txt .

# Install dependencies
RUN pip install -r requirements.txt

# Copy application code
COPY . .

# Set environment variables
ENV APP_ENV=production

# Expose a port
EXPOSE 8000

# Set default command
CMD ["python3", "app.py"]
```

### Build Process

1. Write a Dockerfile (the image definition)
2. Build the image using the Dockerfile
3. Push the image to Docker Hub with a unique tag
4. Pull and deploy the image on another machine as a container
