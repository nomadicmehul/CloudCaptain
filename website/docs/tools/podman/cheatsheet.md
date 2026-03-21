---
title: "Podman Cheat Sheet"
sidebar_label: "Quick Reference"
description: "Podman CLI commands, pod management, rootless operations, and Docker equivalents"
sidebar_position: 2
---

# Podman Cheat Sheet

A quick reference for Podman commands with Docker equivalents and common workflows.

## Installation

```bash
# Ubuntu/Debian
sudo apt-get install podman podman-compose

# RHEL/CentOS
sudo dnf install podman podman-compose

# Fedora
sudo dnf install podman

# macOS (via Homebrew)
brew install podman

# Verify installation
podman --version
podman info
```

## Container Management

### Run Containers

```bash
# Basic container run
podman run alpine echo "Hello World"

# Interactive container
podman run -it alpine sh

# Detached (background)
podman run -d --name myapp nginx:latest

# With port mapping
podman run -d -p 8080:80 --name web nginx:latest

# With environment variables
podman run -d -e DATABASE_URL=postgres://localhost \
  --name app myapp:1.0

# With volume mount
podman run -d -v /data:/app/data --name app myapp:1.0

# With resource limits
podman run -d --memory 512m --cpus 1.0 myapp:1.0

# With hostname and DNS
podman run -d --hostname myhost --dns 8.8.8.8 myapp:1.0
```

### List Containers

```bash
# List running containers
podman ps

# List all containers (including stopped)
podman ps -a

# List with custom format
podman ps --format "table {{.Names}}\t{{.Status}}"

# Get container ID only
podman ps -q
```

### Container Lifecycle

```bash
# Start stopped container
podman start container-id

# Stop running container (graceful)
podman stop container-id

# Stop with timeout
podman stop -t 10 container-id

# Kill container (force)
podman kill container-id

# Restart container
podman restart container-id

# Pause container
podman pause container-id

# Unpause container
podman unpause container-id

# Remove container
podman rm container-id

# Remove stopped containers
podman container prune

# Remove container and its image
podman rm -f container-id && podman rmi image:tag
```

### Container Information

```bash
# View container logs
podman logs container-id

# Follow logs (tail -f style)
podman logs -f container-id

# Last 50 lines
podman logs --tail 50 container-id

# Show timestamps
podman logs -t container-id

# Inspect container details
podman inspect container-id

# Get IP address
podman inspect container-id --format='{{.NetworkSettings.IPAddress}}'

# Get status
podman inspect container-id --format='{{.State.Status}}'

# Get environment variables
podman exec container-id env
```

### Execute Commands

```bash
# Run command in running container
podman exec container-id ls -la /app

# Interactive execution
podman exec -it container-id bash

# Run as specific user
podman exec -u nobody container-id whoami

# With environment variable
podman exec -e MYVAR=value container-id env | grep MYVAR
```

## Image Management

### Search and Pull

```bash
# Search for image
podman search nginx

# Search in specific registry
podman search quay.io/nginx

# Pull image
podman pull nginx:latest

# Pull with digest
podman pull nginx@sha256:abc123...

# Pull from private registry
podman pull registry.company.com/myapp:1.0
```

### List and Remove Images

```bash
# List images
podman images

# List with filter
podman images --filter reference=nginx

# Get image ID
podman images --format='{{.ID}}' nginx

# Remove image
podman rmi nginx:latest

# Remove unused images
podman image prune

# Remove all images
podman rmi -a
```

### Image Information

```bash
# Inspect image details
podman inspect nginx:latest

# Show image history
podman history nginx:latest

# Get image digest
podman inspect nginx:latest --format='{{.Digest}}'

# Show image layers
podman image tree nginx:latest
```

### Build Images

```bash
# Build from Dockerfile
podman build -t myapp:1.0 .

# Build with specific Dockerfile
podman build -f Dockerfile.prod -t myapp:1.0 .

# Build with build arguments
podman build --build-arg VERSION=1.0 -t myapp:1.0 .

# Build without cache
podman build --no-cache -t myapp:1.0 .

# Build with custom registry
podman build -t registry.example.com/myapp:1.0 .
```

### Push Images

```bash
# Push to registry
podman push myapp:1.0

# Push to specific registry
podman push myapp:1.0 quay.io/myrepo/myapp:1.0

# Tag before push
podman tag myapp:1.0 registry.example.com/myapp:1.0
podman push registry.example.com/myapp:1.0

# Push with credentials
podman login registry.example.com
podman push registry.example.com/myapp:1.0
```

## Pod Management

### Pod Lifecycle

```bash
# Create pod
podman pod create --name web-app

# Create pod with port mapping
podman pod create --name web-app -p 8080:80

# Create pod with shared volume
podman pod create --name myapp -v /data:/data

# List pods
podman pod ls

# Show pod details
podman pod inspect web-app

# Stop pod
podman pod stop web-app

# Start pod
podman pod start web-app

# Restart pod
podman pod restart web-app

# Remove pod
podman pod rm web-app

# Remove pod and containers
podman pod rm -f web-app
```

### Pod Operations

```bash
# Run container in pod
podman run -d --pod web-app nginx

# Run with name in pod
podman run -d --pod web-app --name nginx nginx

# List containers in pod
podman ps --filter pod=web-app

# Pod logs (all containers)
podman pod logs web-app

# Stop all containers in pod
podman pod stop web-app
```

## Volume Management

### Create and List Volumes

```bash
# Create named volume
podman volume create mydata

# List volumes
podman volume ls

# Inspect volume
podman volume inspect mydata

# Remove volume
podman volume rm mydata

# Remove unused volumes
podman volume prune
```

### Mount Volumes

```bash
# Mount named volume
podman run -d -v mydata:/data myapp:1.0

# Bind mount (host path)
podman run -d -v /host/path:/container/path myapp:1.0

# Read-only mount
podman run -d -v /data:/app/data:ro myapp:1.0

# Multiple volumes
podman run -d -v vol1:/data1 -v vol2:/data2 myapp:1.0
```

## Network Management

### Network Operations

```bash
# List networks
podman network ls

# Create custom network
podman network create mynet

# Inspect network
podman network inspect mynet

# Remove network
podman network rm mynet

# Connect container to network
podman network connect mynet container-id

# Disconnect container from network
podman network disconnect mynet container-id
```

### Port Mapping

```bash
# Single port
podman run -d -p 8080:80 nginx

# Multiple ports
podman run -d -p 8080:80 -p 8443:443 nginx

# Specific IP and port
podman run -d -p 127.0.0.1:8080:80 nginx

# UDP port
podman run -d -p 5353:53/udp dns-app

# Publish all ports
podman run -d -P myapp:1.0
```

## Rootless Podman

### Setup

```bash
# Migrate to rootless mode
podman system migrate

# Check if rootless
podman info | grep -i rootless

# Verify user namespace
podman run alpine cat /proc/self/uid_map

# Check running user
podman run alpine whoami  # Shows 'root' inside
id  # Shows actual user outside
```

### Rootless Networking

```bash
# Port forwarding (rootless)
podman run -d -p 127.0.0.1:8080:80 nginx

# Non-privileged ports (1024+)
podman run -d -p 8080:8080 myapp:1.0

# Slirp4netns (default rootless networking)
podman run -d --network=slirp4netns:port_handler=rootlesskit \
  -p 8080:80 nginx
```

## Systemd Integration

### Generate and Install Units

```bash
# Generate systemd unit for container
podman generate systemd --name myapp > myapp.service

# Install unit file
sudo cp myapp.service /etc/systemd/system/

# Generate pod unit
podman generate systemd --pod-prefix=pod web-app > pod-web-app.service

# Reload systemd
sudo systemctl daemon-reload
```

### Manage with Systemd

```bash
# Enable container at boot
sudo systemctl enable myapp.service

# Start service
sudo systemctl start myapp

# Status
systemctl status myapp

# Logs
journalctl -u myapp -f

# Stop service
sudo systemctl stop myapp

# Disable at boot
sudo systemctl disable myapp.service
```

## Docker Compose with Podman

### Installation and Setup

```bash
# Install Podman Compose
pip install podman-compose

# Or with Podman 4.0+, use native compose
podman compose --version
```

### Compose Operations

```bash
# Start services
podman-compose -f docker-compose.yml up

# Start in background
podman-compose -f docker-compose.yml up -d

# Stop services
podman-compose -f docker-compose.yml down

# View logs
podman-compose -f docker-compose.yml logs

# Follow logs
podman-compose -f docker-compose.yml logs -f

# Run single service
podman-compose -f docker-compose.yml up service-name

# Build images
podman-compose -f docker-compose.yml build

# Rebuild without cache
podman-compose -f docker-compose.yml build --no-cache

# Remove volumes
podman-compose -f docker-compose.yml down -v

# Execute command
podman-compose -f docker-compose.yml exec service-name cmd
```

## Troubleshooting

### Common Commands

```bash
# System information
podman system info

# Check system compatibility
podman system check

# Resolve daemon connectivity issues (rootless)
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus

# View pod and container stats
podman stats

# Get container resource usage
podman stats --no-stream

# Debug container startup
podman run --rm -it --entrypoint sh myapp:1.0

# Check container capabilities
podman run --rm myapp:1.0 capsh --print

# Inspect container filesystem
podman run --rm -it -v my-vol:/vol myapp:1.0 ls -la /vol
```

### Cleanup

```bash
# Remove all stopped containers
podman container prune

# Remove all unused images
podman image prune

# Remove all unused volumes
podman volume prune

# Remove all unused networks
podman network prune

# Full system cleanup
podman system prune -a
```

## Docker to Podman Command Reference

| Operation | Docker | Podman |
|-----------|--------|--------|
| Run container | `docker run` | `podman run` |
| List containers | `docker ps` | `podman ps` |
| Stop container | `docker stop` | `podman stop` |
| Remove container | `docker rm` | `podman rm` |
| Build image | `docker build` | `podman build` |
| Push image | `docker push` | `podman push` |
| Create network | `docker network create` | `podman network create` |
| Create volume | `docker volume create` | `podman volume create` |
| Compose | `docker-compose` | `podman-compose` |
| Systemd | Manual service file | `podman generate systemd` |

## Quick Workflows

### Deploy Web Application

```bash
# Build image
podman build -t myapp:1.0 .

# Run container
podman run -d --name myapp -p 8080:80 -v config:/etc/myapp myapp:1.0

# Check status
podman ps | grep myapp

# View logs
podman logs -f myapp

# Stop and remove
podman stop myapp
podman rm myapp
```

### Multi-Container Application (Compose)

```bash
# Create docker-compose.yml with services
cat > docker-compose.yml << EOF
version: '3.8'
services:
  web:
    build: .
    ports: ["8080:8080"]
  db:
    image: postgres:13
    environment:
      POSTGRES_PASSWORD: secret
EOF

# Start all services
podman-compose up -d

# View logs
podman-compose logs -f

# Stop all services
podman-compose down
```

### Create Systemd Service

```bash
# Create and run container
podman run -d --name myapp myapp:1.0

# Generate systemd unit
podman generate systemd --name myapp > myapp.service

# Install and enable
sudo cp myapp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now myapp

# Manage
sudo systemctl status myapp
```

## Tips and Best Practices

1. **Use named containers** — Makes management easier
2. **Set resource limits** — Prevent runaway containers
3. **Use health checks** — Ensure container readiness
4. **Implement logging** — Keep logs accessible
5. **Use secrets management** — Don't hardcode credentials
6. **Regular cleanup** — Use `podman system prune` regularly
7. **Test rootless** — Validate security benefits
8. **Document port mappings** — Know what ports are exposed
9. **Use pod grouping** — Organize related containers
10. **Integrate with systemd** — Ensure container persistence

## Common Issues and Solutions

| Issue | Solution |
|-------|----------|
| Port already in use | Use different port with `-p` or check existing containers |
| Permission denied | Run with `sudo` or verify rootless setup |
| Image pull fails | Check network, verify image exists, check registry credentials |
| Container exits immediately | Check logs with `podman logs` |
| Storage space full | Run `podman system prune -a` to free space |

## Summary

Podman provides Docker-compatible container management with modern architecture. Most Docker commands work directly with Podman, making migration straightforward. The daemonless, rootless approach offers enhanced security and simpler deployment.
