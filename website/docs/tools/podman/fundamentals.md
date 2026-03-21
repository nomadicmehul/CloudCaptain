---
title: "Podman Fundamentals"
sidebar_label: "Fundamentals"
description: "Podman architecture, rootless containers, pods, systemd integration, and migration from Docker"
sidebar_position: 1
---

# Podman Fundamentals

Podman is a daemonless, open-source container engine for Linux that manages containers and container images. Unlike Docker, Podman doesn't require a daemon running as superuser, making it a more secure and flexible alternative for modern container workloads.

## What is Podman?

Podman (Pod Manager) is a container runtime developed by Red Hat that implements the Open Containers Initiative (OCI) standard. It provides drop-in compatibility with Docker while introducing significant architectural improvements.

**Key definition:** Podman is "a daemonless, open source, Linux native tool designed to make it easy to find, run, build, share and deploy applications using Open Containers Initiative (OCI) Containers and Container Images."

## Podman vs Docker: Key Differences

### Architecture Comparison

**Docker Architecture:**
```
┌─────────────────────────────────────────────────────────┐
│ Client (docker CLI)                                     │
└─────────────────┬───────────────────────────────────────┘
                  │ (REST API)
┌─────────────────▼───────────────────────────────────────┐
│ Docker Daemon (dockerd) — runs as root                  │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ Container Runtime (containerd)                      │ │
│ │ - Container lifecycle management                    │ │
│ │ - Process isolation                                 │ │
│ └─────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

**Podman Architecture:**
```
┌──────────────────────────────────────────────────────────┐
│ Podman CLI (runs as user — no daemon required)          │
│ - Direct container management                            │
│ - Communicates directly with libpod                      │
└──────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ libpod library (Podman runtime)                          │
│ - Container lifecycle management                         │
│ - OCI container runtime interaction                      │
└──────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ OCI Runtime (runc, crun)                                 │
│ - Actual container execution                             │
└──────────────────────────────────────────────────────────┘
```

### Major Differences

| Feature | Docker | Podman |
|---------|--------|--------|
| **Architecture** | Daemon-based (Docker daemon) | Daemonless (direct libpod) |
| **Root requirement** | Daemon runs as root | Rootless by default |
| **Security** | All operations require root | Per-user containers |
| **Container children** | Child processes of daemon | Durable, independent |
| **Container format** | Docker format | OCI standard (compatible with Docker) |
| **Orchestration** | Docker Swarm (monolithic) | Kubernetes-native |
| **API** | REST API over socket | REST API optional; native libpod |
| **Pod support** | Limited (via docker-compose) | First-class pods |
| **Systemd integration** | None | Native systemd integration |

## Rootless Containers

### What is Rootless?

Rootless containers run without requiring superuser (root) privileges. Processes inside rootless containers run under the user's UID, not root's UID (0).

**Benefits:**
- **Enhanced security** — Even if a container escapes, the attacker has user privileges, not root
- **UID namespace isolation** — Processes in containers appear as root inside (UID 0) but map to user outside
- **Multi-tenancy** — Multiple users can safely run containers on the same system
- **Reduced attack surface** — No daemon privilege escalation vector

### How Rootless Works

Podman uses Linux namespaces and user mapping:

```
Inside Container (as seen by processes)
┌────────────────────────────────────┐
│ PID 1  (appears as UID 0 — root)   │
│ PID 2  (appears as UID 0 — root)   │
└────────────────────────────────────┘
         ↓ (user namespace mapping)
Outside Container (on host)
┌────────────────────────────────────┐
│ PID 12345 (UID 1000 — alice)       │
│ PID 12346 (UID 1000 — alice)       │
└────────────────────────────────────┘
```

### Rootless Setup

```bash
# Enable rootless mode for current user
podman system migrate

# Run rootless container
podman run -it alpine sh

# Verify rootless execution
podman run alpine id
# Output: uid=0(root) gid=0(root) groups=0(root)
# But on host, the process runs as current user

# Check user mapping
podman run alpine cat /proc/self/uid_map
# Shows 0       1000       65536 (maps container 0 to host 1000+)
```

## Pods: Podman's Container Groups

### What is a Pod?

A pod is a collection of containers sharing network namespace and storage. This concept is borrowed directly from Kubernetes and enables multi-container applications.

**Pod characteristics:**
- Shared network namespace (same IP address, shared localhost)
- Optional shared storage volumes
- Multiple containers coordinated as a unit
- Optional shared IPC namespace

### Pod vs Single Container

**Single container:**
```
┌─────────────────────────────────────┐
│ Container A                         │
│ ┌───────────────────────────────┐   │
│ │ App Process (port 8080)       │   │
│ └───────────────────────────────┘   │
│ Network: eth0 (172.17.0.2)          │
└─────────────────────────────────────┘
```

**Pod with multiple containers:**
```
┌──────────────────────────────────────────────────┐
│ Pod: web-app                                     │
│ Network: eth0 (172.17.0.2) — SHARED             │
│ ┌──────────────────┐  ┌──────────────────┐      │
│ │ Container: app   │  │ Container: logs  │      │
│ │ Port 8080        │  │ Port 9090        │      │
│ └──────────────────┘  └──────────────────┘      │
│ Both access localhost:8080 (same interface)     │
└──────────────────────────────────────────────────┘
```

### Pod Operations

```bash
# Create a pod
podman pod create --name web-app -p 8080:8080

# Run container in pod
podman run -d --pod web-app nginx
podman run -d --pod web-app --name app-logger fluent-bit

# Pod status
podman pod ls
podman pod inspect web-app

# Stop all containers in pod
podman pod stop web-app

# Remove pod (removes all containers)
podman pod rm web-app
```

### Pod Manifest (Pod Definition)

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: web-app
spec:
  containers:
  - name: nginx
    image: nginx:latest
    ports:
    - containerPort: 80
  - name: app
    image: myapp:1.0
    ports:
    - containerPort: 8080
  # All containers share network namespace
  # They can communicate via localhost
```

## Systemd Integration

### What is Systemd Integration?

Podman can generate and manage systemd service units, treating containers as first-class systemd services. Containers restart on reboot and follow standard systemd conventions.

### Benefits

- **Persistence** — Containers survive system reboot
- **Standard management** — Use `systemctl` to manage containers
- **Logging integration** — Container logs appear in `journalctl`
- **Dependency management** — Order service startup with `After=`, `Wants=`
- **Resource limits** — Control CPU, memory via systemd directives

### Generate Systemd Unit

```bash
# Create a container
podman run -d --name nginx -p 80:80 nginx

# Generate systemd unit file
podman generate systemd --name nginx > nginx.service

# Result (example):
cat nginx.service
```

```ini
# /etc/systemd/system/nginx.service
[Unit]
Description=Podman container-nginx
Documentation=man:podman-generate-systemd(1)
Wants=network-online.target
After=network-online.target
RequiresMuffins=podman.service

[Service]
Environment="PODMAN_SYSTEMD_UNIT=%n"
Restart=on-failure
ExecStart=/usr/bin/podman start -a nginx
ExecStop=/usr/bin/podman stop -t 10 nginx
Type=notify
NotifyAccess=main

[Install]
WantedBy=multi-user.target
```

### Enable and Manage

```bash
# Copy unit to systemd directory
sudo cp nginx.service /etc/systemd/system/

# Reload systemd daemon
sudo systemctl daemon-reload

# Enable service (start on boot)
sudo systemctl enable nginx.service

# Start service
sudo systemctl start nginx

# Check status
systemctl status nginx

# View logs
journalctl -u nginx -f

# Stop service
sudo systemctl stop nginx
```

## Buildah and Skopeo: Complementary Tools

### Buildah: Container Image Building

Buildah is a tool for building OCI-compliant container images. It provides lower-level control than Podman's `build` command.

```bash
# Build image with Buildah
buildah bud -t myapp:1.0 .

# Lower-level operations
ctr=$(buildah from alpine)
buildah run $ctr apk add --no-cache curl
buildah copy $ctr ./app /app
buildah entrypoint $ctr ["/app/start.sh"]
buildah commit $ctr myapp:1.0
```

### Skopeo: Image Registry Operations

Skopeo inspects and copies container images between registries without running a daemon.

```bash
# Inspect remote image
skopeo inspect docker://docker.io/library/nginx:latest

# Copy image between registries
skopeo copy docker://docker.io/library/nginx:latest \
           docker://quay.io/myrepo/nginx:latest

# Delete image from registry
skopeo delete docker://quay.io/myrepo/nginx:latest
```

### Tooling Relationship

```
┌──────────────────────────────────────────────────┐
│ Podman Ecosystem                                 │
├──────────────────────────────────────────────────┤
│ Podman       — Container runtime + management    │
│ Buildah      — Low-level image building          │
│ Skopeo       — Registry operations (no daemon)   │
│ Podman Compose — Docker Compose compatibility    │
└──────────────────────────────────────────────────┘
```

## Migration from Docker

### Command Compatibility

Most Docker commands work 1:1 with Podman. The primary difference is the executable name.

**Docker to Podman mapping:**

```bash
# Basic equivalence
docker run → podman run
docker build → podman build
docker ps → podman ps
docker images → podman images
docker volume → podman volume
docker network → podman network
docker inspect → podman inspect
docker exec → podman exec
docker logs → podman logs
```

### Full CLI Compatibility

```bash
# Alias for compatibility
alias docker=podman

# Or create symlink
ln -s $(which podman) /usr/bin/docker
```

### Compose File Migration

Docker Compose files work with Podman Compose:

```bash
# Install Podman Compose
pip install podman-compose

# Use like Docker Compose
podman-compose -f docker-compose.yml up
podman-compose -f docker-compose.yml down

# Or with Podman's native compose (requires Podman 4.0+)
podman compose -f docker-compose.yml up
```

### Key Migration Considerations

1. **Socket location** — Docker: `/var/run/docker.sock`; Podman: `/run/user/1000/podman/podman.sock` (rootless)
2. **Root vs rootless** — Docker runs all as root; Podman defaults to rootless
3. **Pod concept** — Docker has minimal pod support; Podman has native pod support
4. **Systemd** — Podman integrates seamlessly; Docker requires systemd service files
5. **Daemon** — Docker requires daemon; Podman is daemonless

### Migration Checklist

- [ ] Install Podman
- [ ] Test basic container operations
- [ ] Verify image compatibility
- [ ] Test rootless containers
- [ ] Migrate docker-compose.yml files
- [ ] Update any socket/daemon references
- [ ] Configure systemd units for persistent containers
- [ ] Test pod functionality
- [ ] Update CI/CD pipelines
- [ ] Document team transition

## Security in Podman

### Default Security Model

```
Rootless Podman (Default)
├── User namespace isolation (UID mapping)
├── No escalation privilege by default
├── Per-user container isolation
└── Lower attack surface

Rootful Podman
├── Full root access inside container
├── SELinux integration available
├── More powerful but requires root
└── Use only when necessary
```

### SELinux Integration

Podman supports SELinux labels:

```bash
# Run container with SELinux labels
podman run -d --security-opt label=type:svirt_apache_t \
           -p 80:80 nginx

# List available labels
semanage login -l
```

## Exercises

### Exercise 1: Basic Podman Workflow
1. Pull an image: `podman pull alpine`
2. Run container interactively: `podman run -it alpine sh`
3. List running containers: `podman ps`
4. Stop container: `podman stop <container-id>`

### Exercise 2: Rootless Containers
1. Enable rootless mode
2. Run a container and verify it runs under your user UID
3. Check process ownership on the host system
4. Confirm containers don't require sudo

### Exercise 3: Pod Management
1. Create a pod with multiple containers
2. Verify containers can communicate via localhost
3. Inspect the pod configuration
4. Stop the entire pod with a single command

### Exercise 4: Systemd Integration
1. Run a container
2. Generate systemd unit file
3. Install unit file in /etc/systemd/system/
4. Enable and start via systemctl
5. Verify container survives system reboot (in test environment)

### Exercise 5: Compose Migration
1. Create a docker-compose.yml with multiple services
2. Convert to Podman Compose
3. Start services with `podman-compose up`
4. Verify all services communicate correctly

## Summary

Podman represents a modern approach to container management with these core strengths:

- **Daemonless architecture** — Simpler, more secure design
- **Rootless default** — Better security posture out-of-the-box
- **First-class pods** — Native Kubernetes pod support
- **Systemd integration** — Seamless Linux integration
- **Docker compatibility** — Drop-in replacement for most workloads
- **Open standards** — OCI-compliant, future-proof

For teams moving to Kubernetes or prioritizing security and simplicity, Podman is an excellent choice.
