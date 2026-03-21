---
title: "Podman"
sidebar_label: "Podman Overview"
description: "Daemonless, rootless container engine — a secure Docker alternative"
sidebar_position: 0
---

# Podman

Podman is a daemonless, rootless container engine that provides Drop-in Docker compatibility with enhanced security and modern Linux integration. Unlike Docker, Podman doesn't require a background daemon and defaults to rootless containers for non-root users.

## What is Podman?

Podman (Pod Manager) is an open-source container runtime developed by Red Hat that implements the Open Containers Initiative (OCI) standard. It enables you to find, run, build, share, and deploy applications using OCI containers.

**Key benefits:**
- **Daemonless** — No background daemon required
- **Rootless** — Containers run under user UID by default
- **Pod-native** — First-class support for multi-container pods
- **Systemd integration** — Treat containers as systemd services
- **Docker-compatible** — Nearly all Docker commands work as-is
- **Kubernetes-ready** — Pod design mirrors Kubernetes concepts

## Documentation

| Guide | Description |
|:------|:------------|
| [Podman Fundamentals](/docs/tools/podman/fundamentals) | Architecture, rootless containers, pods, systemd integration, Buildah, Skopeo, and migration from Docker |
| [Podman Cheat Sheet](/docs/tools/podman/cheatsheet) | Command reference, pod and volume management, systemd integration, and Docker equivalents |

## Quick Examples

### Run a Container

```bash
# Interactive shell
podman run -it alpine sh

# Background daemon
podman run -d --name nginx -p 8080:80 nginx

# With volume
podman run -d -v mydata:/data postgres:13
```

### Create a Pod

```bash
# Create pod
podman pod create --name web-app -p 8080:80

# Add containers to pod
podman run -d --pod web-app nginx
podman run -d --pod web-app --name app myapp:1.0

# All containers share network (localhost communication)
```

### Use Docker Compose

```bash
# Install Podman Compose
pip install podman-compose

# Run Compose
podman-compose -f docker-compose.yml up -d
```

### Systemd Service

```bash
# Generate systemd unit
podman generate systemd --name nginx > nginx.service

# Install and enable
sudo cp nginx.service /etc/systemd/system/
sudo systemctl enable --now nginx

# Manage
sudo systemctl status nginx
```

## Podman vs Docker

| Feature | Podman | Docker |
|---------|--------|--------|
| **Architecture** | Daemonless | Daemon-based |
| **Rootless default** | Yes | No |
| **Pod support** | First-class | Limited |
| **Systemd integration** | Native | Manual |
| **Security model** | User namespace isolation | Root by default |
| **Command compatibility** | Nearly 100% Docker | N/A |

## Why Choose Podman?

1. **Enhanced Security** — Rootless containers reduce privilege escalation risks
2. **Simpler Architecture** — No daemon means simpler debugging and deployment
3. **Kubernetes Alignment** — Native pod support mirrors Kubernetes design
4. **Linux Integration** — Systemd integration for container persistence
5. **Migration Path** — Drop-in Docker replacement for most workloads

## Getting Started

1. **[Install Podman](https://podman.io/getting-started/installation/)** — Straightforward on most Linux distros
2. **Learn fundamentals** — [Podman Fundamentals](/docs/tools/podman/fundamentals)
3. **Master commands** — [Podman Cheat Sheet](/docs/tools/podman/cheatsheet)
4. **Enable rootless** — Run containers without root privileges
5. **Create pods** — Experience Kubernetes-like pod grouping

## Common Tasks

- **Build images** — `podman build -t myapp:1.0 .`
- **Run containers** — `podman run -d myapp:1.0`
- **Manage pods** — `podman pod create/start/stop`
- **Push to registry** — `podman push myapp:1.0 quay.io/myrepo/myapp:1.0`
- **Docker Compose** — `podman-compose -f docker-compose.yml up`

## Tools in the Podman Ecosystem

- **Podman** — Container runtime and management
- **Buildah** — Low-level container image building
- **Skopeo** — Registry operations without daemon
- **Podman Compose** — Docker Compose compatibility

## Next Steps

- **[Podman Fundamentals](/docs/tools/podman/fundamentals)** — Deep dive into architecture, rootless, pods, and systemd
- **[Podman Cheat Sheet](/docs/tools/podman/cheatsheet)** — Command reference and quick workflows
- **[Official Documentation](https://docs.podman.io/)** — Complete reference
- **[Red Hat Podman Guide](https://developers.redhat.com/articles/podman-basics-cheat-sheet)** — Comprehensive resource

## Contributing

Have Podman workflows, tips, or best practices to share? [Contribute](https://github.com/nomadicmehul/CloudCaptain) to CloudCaptain!
