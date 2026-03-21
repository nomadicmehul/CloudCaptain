---
title: "Docker Interview Questions"
sidebar_label: "Interview Questions"
description: "50+ Docker interview questions and answers — from beginner to advanced, covering architecture, networking, security, and orchestration"
sidebar_position: 8
---

# Docker Interview Questions and Answers

A comprehensive guide to Docker interview questions covering beginner, intermediate, and advanced levels. Master the fundamentals, architecture, networking, and security concepts to ace your Docker interviews.

---

## Beginner Level (Questions 1-15)

### 1. What is Docker?

Docker is a containerization platform that enables developers to package applications with all their dependencies (libraries, runtime, tools) into a standardized unit called a container. Docker ensures applications run consistently across different environments—from development to production—by isolating them from the underlying system.

### 2. What is a container?

A container is a lightweight, standalone, executable package that includes everything needed to run an application: code, runtime, system tools, libraries, and settings. Containers are isolated from each other and from the host system, ensuring predictable behavior regardless of where they're deployed. They're similar to virtual machines but much more efficient.

### 3. What is a Docker image?

A Docker image is a read-only blueprint or template used to create containers. It contains the application code, runtime, libraries, and dependencies stacked in layers. Images are built from a Dockerfile and can be stored in registries like Docker Hub. When you run an image, it creates a container—a running instance of that image.

### 4. What is a Dockerfile?

A Dockerfile is a text file containing a series of instructions to build a Docker image. Each instruction creates a new layer in the image. Common instructions include `FROM` (base image), `COPY` (copy files), `RUN` (execute commands), `EXPOSE` (expose ports), and `ENTRYPOINT` (set default command). The `docker build` command reads a Dockerfile and creates an image.

### 5. What is Docker Hub?

Docker Hub is the official public registry for Docker images. It's a centralized repository where developers can push, pull, and share Docker images. It hosts millions of public images, including official images for popular software like Ubuntu, Node.js, PostgreSQL, and MySQL. You can also create private repositories on Docker Hub for your own images.

### 6. What is the difference between a container and a virtual machine?

| Aspect | Container | Virtual Machine |
|--------|-----------|-----------------|
| **Size** | Lightweight (MBs) | Heavy (GBs) |
| **Boot time** | Seconds | Minutes |
| **OS overhead** | Shares host OS kernel | Runs full OS |
| **Isolation** | Process-level isolation | Hardware-level isolation |
| **Performance** | Near-native performance | Some overhead |
| **Portability** | Highly portable | Less portable |

### 7. What are the main components of Docker architecture?

Docker architecture has three main components:
- **Docker Client**: Sends commands to the Docker daemon (e.g., `docker build`, `docker run`)
- **Docker Daemon**: Runs in the background and manages Docker objects (images, containers, networks, volumes)
- **Docker Registry**: Stores and distributes Docker images (e.g., Docker Hub, private registries)

The client communicates with the daemon via REST API and socket connections.

### 8. What is the difference between Docker CE and Docker EE?

**Docker Community Edition (CE)** is the free, open-source version suitable for developers and small teams. It receives updates regularly but has minimal support. **Docker Enterprise Edition (EE)** (now called Docker Enterprise) is a paid version with advanced features like integrated image scanning, role-based access control (RBAC), and priority support. It's designed for enterprise deployments requiring security and compliance.

### 9. What command lists all running containers?

```bash
docker ps
```

This lists containers that are currently running. To view all containers (including stopped ones), use:

```bash
docker ps -a
```

You can also format the output with `--format` flag or filter with `--filter` for more control.

### 10. How do you run a container in detached mode?

Use the `-d` or `--detach` flag with `docker run`. This starts the container in the background and returns control to the terminal:

```bash
docker run -d nginx:latest
```

The command prints the container ID. In attached mode (without `-d`), you'd see the container's output in your terminal.

### 11. What happens when you run `docker run`?

The `docker run` command performs several steps:
1. Pulls the image from a registry (if not already present locally)
2. Creates a new container from the image
3. Allocates a unique container ID and filesystem
4. Creates a network interface and connects it to the Docker network
5. Starts the container and executes the default command (ENTRYPOINT or CMD)
6. Attaches to the container (unless `-d` is used)

### 12. How do you stop and remove a container?

To stop a running container gracefully (sends SIGTERM):
```bash
docker stop <container_id_or_name>
```

To force stop a container (sends SIGKILL):
```bash
docker kill <container_id_or_name>
```

To remove a stopped container:
```bash
docker rm <container_id_or_name>
```

To stop and remove in one command:
```bash
docker rm -f <container_id_or_name>
```

### 13. What is a Docker registry?

A Docker registry is a repository that stores and distributes Docker images. **Docker Hub** is the default public registry, but you can use other public registries (Quay.io, ECR, GCR) or run a private registry locally. Registries are organized into repositories, and each repository can contain multiple image versions tagged with different tags.

### 14. On what circumstances will you lose data stored in a container?

Data stored in a container's writable layer is lost when the container is deleted. This happens when:
- You delete the container with `docker rm`
- The container crashes and is replaced
- The host system fails

To persist data, use **volumes** or **bind mounts** to store data outside the container filesystem. Volumes are stored in the host's filesystem (usually `/var/lib/docker/volumes`) and survive container deletion.

### 15. What is the purpose of the `docker inspect` command?

The `docker inspect` command returns detailed, low-level information about a Docker object (container, image, volume, network, etc.) in JSON format. It displays information such as:
- Container configuration and settings
- Mounted volumes and their paths
- Environment variables
- Port mappings
- Network configuration
- Image layers and history

Example: `docker inspect <container_id>` returns complete metadata about the container.

---

## Intermediate Level (Questions 16-35)

### 16. What is the difference between CMD and ENTRYPOINT?

**CMD** specifies default arguments for the container. It can be overridden when running the container.
**ENTRYPOINT** specifies the main command to execute. It's not overridden easily by default arguments.

```dockerfile
# Using CMD (can be overridden)
FROM ubuntu
CMD ["echo", "Hello World"]
```

Running `docker run myimage` prints "Hello World", but `docker run myimage echo "Goodbye"` prints "Goodbye".

```dockerfile
# Using ENTRYPOINT (harder to override, use --entrypoint flag)
FROM ubuntu
ENTRYPOINT ["echo"]
CMD ["Hello World"]
```

Best practice: Use `ENTRYPOINT` for the main application and `CMD` for default arguments.

### 17. What is the difference between COPY and ADD?

| Feature | COPY | ADD |
|---------|------|-----|
| **Function** | Copies files/directories from build context | Copies files, can also extract archives and fetch URLs |
| **Archives** | No automatic extraction | Automatically extracts tar.gz files |
| **Remote URLs** | Not supported | Supports remote URLs |
| **Use case** | General-purpose file copying | Legacy, more complex scenarios |

```dockerfile
COPY app.js /app/
ADD application.tar.gz /app/
ADD https://example.com/file.tar.gz /app/
```

Best practice: Use `COPY` for most cases; it's more predictable and transparent.

### 18. What are Docker namespaces?

Docker namespaces are kernel features that provide process isolation. They partition system resources so containers see only their own resources. Main namespaces:

- **PID namespace**: Isolates process IDs; each container has its own PID 1
- **Network namespace**: Isolates networking (IP address, ports, routing)
- **Mount namespace**: Isolates filesystems
- **IPC namespace**: Isolates inter-process communication
- **UTS namespace**: Isolates hostname and domainname
- **User namespace**: Isolates user and group IDs

Namespaces enable containers to run isolated from each other while sharing the host kernel.

### 19. What are Docker cgroups?

Control groups (cgroups) are kernel features that limit and monitor resource usage of processes. Docker uses cgroups to enforce resource limits on containers:

- **Memory**: Limit maximum RAM usage
- **CPU**: Limit CPU shares and quotas
- **Block I/O**: Limit disk read/write bandwidth
- **Network**: Control network traffic (with tc commands)
- **Devices**: Control device access

Example:
```bash
docker run -m 512m --cpus 1 myimage
```
This limits the container to 512MB RAM and 1 CPU core.

### 20. What is Docker Compose?

Docker Compose is a tool for defining and running multi-container Docker applications. You describe services in a `docker-compose.yml` file (networks, volumes, environment variables) and run the entire stack with a single command. It simplifies development by allowing you to manage multiple interconnected containers as a single application.

```yaml
version: '3'
services:
  web:
    image: nginx:latest
  db:
    image: postgres:latest
    environment:
      POSTGRES_PASSWORD: secret
```

Run with `docker-compose up` to start all services.

### 21. Explain Docker networking types

Docker provides several networking modes:

- **Bridge (default)**: Containers on the same bridge can communicate; good for single-host scenarios
- **Host**: Container shares the host's network stack; no network isolation, best performance
- **None**: Container has no network interface; useful for processing-only containers
- **Overlay**: Multi-host networking across Docker Swarm or Kubernetes clusters
- **Macvlan**: Assigns MAC addresses to containers, making them appear as physical devices

```bash
docker run --network host myimage
docker run --network bridge myimage
```

### 22. What are Docker volumes and why are they important?

Docker volumes are the preferred way to persist data generated or used by containers. They're stored outside the container's filesystem and can outlive the container. Importance:

- **Persistence**: Data survives container deletion
- **Sharing**: Multiple containers can share the same volume
- **Decoupling**: Separates application logic from data storage
- **Performance**: Better I/O performance than bind mounts
- **Backup**: Easier to backup volumes than container filesystems

```bash
docker volume create myvolume
docker run -v myvolume:/data myimage
```

### 23. What is the difference between a bind mount and a volume?

| Feature | Volume | Bind Mount |
|---------|--------|-----------|
| **Location** | Managed by Docker (`/var/lib/docker/volumes/`) | Any path on host system |
| **Creation** | Created by Docker | Already exists on host |
| **Performance** | Better on all platforms | Better on Mac/Windows |
| **Use case** | Production, data sharing | Development, hot-reload |
| **Syntax** | `-v myvolume:/data` or `--mount type=volume` | `-v /host/path:/container/path` |

Volumes are preferred for production; bind mounts are useful for development.

### 24. What is a multi-stage build and why use it?

A multi-stage build uses multiple `FROM` instructions in a single Dockerfile. Each stage can copy artifacts from previous stages. This reduces final image size by excluding build dependencies.

```dockerfile
# Stage 1: Build
FROM golang:1.20 AS builder
WORKDIR /app
COPY . .
RUN go build -o myapp .

# Stage 2: Runtime
FROM alpine:latest
COPY --from=builder /app/myapp /app/myapp
CMD ["/app/myapp"]
```

Benefits: Smaller images, faster deployments, reduced attack surface.

### 25. How does Docker layer caching work?

Each instruction in a Dockerfile creates a layer. Docker caches layers to speed up builds. When building, Docker checks if the instruction and its context match a previous build:

- If they match, Docker reuses the cached layer
- If they don't match, Docker rebuilds that layer and all subsequent layers
- Once a layer is invalidated, all downstream layers are rebuilt

**Cache-busting**: Changes to `COPY` or `ADD` instructions invalidate caches if files change. Add frequently-changing instructions near the end of the Dockerfile to maximize cache hits.

```dockerfile
FROM ubuntu
RUN apt-get update && apt-get install -y python3  # Cached if unchanged
COPY requirements.txt .
RUN pip install -r requirements.txt                # Invalidated if requirements.txt changes
COPY app.py .
```

### 26. What is the difference between `docker stop` and `docker kill`?

**`docker stop`**: Sends a SIGTERM signal to the main process, giving it 10 seconds to shut down gracefully. Allows cleanup operations (flushing logs, closing connections).

**`docker kill`**: Sends a SIGKILL signal immediately, terminating the process without warning. No graceful shutdown.

```bash
docker stop container_id    # Graceful shutdown
docker kill container_id    # Immediate termination
```

Use `docker stop` for normal operation; use `docker kill` only when a container is unresponsive.

### 27. What is Docker Swarm?

Docker Swarm is Docker's native orchestration platform for managing multiple Docker hosts as a single cluster. It provides:

- **Clustering**: Manage multiple hosts as one
- **Scheduling**: Deploy services across nodes
- **Load balancing**: Distribute traffic automatically
- **High availability**: Automatically replace failed replicas
- **Rolling updates**: Update services without downtime

```bash
docker swarm init               # Initialize Swarm on manager
docker service create ...       # Deploy services across cluster
docker service scale web=5      # Scale to 5 replicas
```

Docker Swarm is simpler than Kubernetes but less feature-rich.

### 28. How can a container restart by itself?

Use the `--restart` flag with `docker run` or the `restart_policy` in docker-compose:

- `no`: Don't restart (default)
- `always`: Always restart if it exits
- `unless-stopped`: Always restart unless explicitly stopped
- `on-failure`: Restart only on non-zero exit code
- `on-failure:max-retries`: Restart up to max-retries times

```bash
docker run --restart always myimage
docker run --restart on-failure:5 myimage  # Restart max 5 times
```

In docker-compose:
```yaml
services:
  web:
    restart_policy:
      condition: on-failure
      max_attempts: 5
```

### 29. What is the difference between `docker run` and `docker create`?

**`docker run`**: Creates a container and starts it immediately. Equivalent to `docker create` followed by `docker start`.

**`docker create`**: Creates a container but doesn't start it. Returns the container ID. Useful when you need to configure the container before starting it.

```bash
docker create --name mycontainer myimage      # Creates but doesn't start
docker start mycontainer                      # Now start it
docker run myimage                            # Create and start immediately
```

### 30. How do you share data between containers?

**Using volumes**:
```bash
docker run -v myvolume:/data container1
docker run -v myvolume:/data container2
```

**Using bind mounts**:
```bash
docker run -v /host/path:/data container1
docker run -v /host/path:/data container2
```

**Using docker-compose** (same volume for multiple services):
```yaml
services:
  web:
    volumes:
      - myvolume:/data
  worker:
    volumes:
      - myvolume:/data
volumes:
  myvolume:
```

All containers can read/write the same data in the shared volume or mount point.

### 31. What is the purpose of .dockerignore?

`.dockerignore` works like `.gitignore` for Docker. It specifies files and directories to exclude from the build context sent to the Docker daemon. This reduces build context size and speeds up builds.

```
# .dockerignore
node_modules/
.git/
.env
README.md
*.log
.DS_Store
```

Without `.dockerignore`, all files in the build directory are sent to the daemon, wasting bandwidth and time even if they're not used in the Dockerfile.

### 32. Explain Docker logging mechanisms

Docker provides several logging drivers:

- **json-file** (default): Logs stored as JSON files on the host
- **syslog**: Sends logs to syslog
- **awslogs**: Sends logs to AWS CloudWatch
- **splunk**: Integrates with Splunk
- **gcplogs**: Google Cloud Logging
- **none**: Disables logging

View logs with `docker logs`:
```bash
docker logs container_id
docker logs -f container_id              # Follow logs
docker logs --tail 50 container_id       # Last 50 lines
```

Configure logging driver in docker-compose:
```yaml
services:
  web:
    logging:
      driver: "awslogs"
      options:
        awslogs-group: "/ecs/web"
```

### 33. What is the difference between `docker exec` and `docker attach`?

**`docker exec`**: Runs a new command in a running container. Useful for running one-off commands or interactive shells:

```bash
docker exec container_id ls -la           # Run command
docker exec -it container_id /bin/bash    # Interactive shell
```

**`docker attach`**: Attaches your terminal to the running container's STDOUT/STDERR. You see the container's output and can send input. Used for interacting with the main process:

```bash
docker attach container_id
```

Key difference: `exec` runs a separate process; `attach` connects to the main process.

### 34. How do you pass environment variables to a container?

Use the `-e` flag or `--env-file` option:

```bash
docker run -e NODE_ENV=production myimage
docker run -e APP_KEY=secret -e DB_HOST=localhost myimage
docker run --env-file .env myimage        # Load from file
```

In docker-compose:
```yaml
services:
  web:
    environment:
      - NODE_ENV=production
      - DATABASE_URL=postgres://db:5432/mydb
    env_file:
      - .env
```

In Dockerfile (build-time):
```dockerfile
ENV NODE_ENV production
```

### 35. What are Docker health checks?

Health checks monitor container status. Docker periodically runs a command and marks the container as healthy or unhealthy:

```dockerfile
FROM ubuntu
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost/ || exit 1
```

In docker-compose:
```yaml
services:
  web:
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 5s
```

Docker uses health status for orchestration decisions (restart, replace unhealthy containers in Swarm/Kubernetes).

---

## Advanced Level (Questions 36-50+)

### 36. How does the Docker overlay network work?

The overlay network is a virtual network that spans multiple Docker hosts, enabling container-to-container communication across machines. It uses:

- **VXLAN tunneling**: Encapsulates container traffic in UDP packets
- **Distributed key-value store** (etcd/Consul): Stores network metadata
- **Ingress load balancer**: Routes external traffic to services

When a container sends traffic to another container on a different host, the overlay network:
1. Encapsulates the packet in VXLAN
2. Routes it through the underlay network to the destination host
3. Decapsulates and delivers to the destination container

Overlay networks require Docker Swarm or Kubernetes for multi-host communication.

### 37. Explain Docker's copy-on-write (CoW) strategy

Docker uses copy-on-write (CoW) to optimize storage and performance. All containers from the same image share the same read-only layers. When a container modifies a file:

1. The container's writable layer intercepts the write request
2. Docker copies the file from the read-only layer to the writable layer
3. The container modifies the copy in its writable layer

Benefits:
- **Reduced storage**: Only modifications consume extra space
- **Faster container startup**: No need to copy entire filesystem
- **Memory efficiency**: Images are memory-mapped, sharing kernel pages

This is why container startup is nearly instant regardless of image size.

### 38. How do you secure a Docker container in production?

Key security practices:

1. **Run as non-root user**: Add a user in Dockerfile and use `USER` instruction
2. **Use read-only filesystem**: `docker run --read-only` or mount tmpfs for writable areas
3. **Limit capabilities**: Drop unnecessary Linux capabilities with `--cap-drop`
4. **Use security options**: `--security-opt apparmor=` or `--security-opt seccomp=`
5. **Resource limits**: Prevent DoS with memory and CPU limits
6. **Scan images**: Use tools like Trivy or Grype for vulnerability scanning
7. **Use secrets management**: Never hardcode secrets; use Docker secrets or external vaults
8. **Enable audit logging**: Track container activities

```dockerfile
FROM ubuntu
RUN useradd -m appuser
USER appuser
```

```bash
docker run --read-only --cap-drop ALL --cpus 1 -m 512m myimage
```

### 39. What is Docker Content Trust?

Docker Content Trust (DCT) enables cryptographic verification of image authenticity and integrity. It:

- **Signs images** at push time with a private key
- **Verifies signatures** at pull time
- **Prevents tampering**: Ensures images haven't been modified
- **Prevents rollback attacks**: Uses timestamping to prevent old image versions

Enable DCT with environment variable:
```bash
export DOCKER_CONTENT_TRUST=1
docker push myimage:latest    # Signs the image
docker pull myimage:latest    # Verifies signature
```

DCT provides supply-chain security for container images.

### 40. How do you implement a CI/CD pipeline with Docker?

A typical CI/CD pipeline with Docker:

```yaml
# GitHub Actions example
name: CI/CD Pipeline
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Build Docker image
        run: docker build -t myapp:${{ github.sha }} .
      - name: Run tests in container
        run: docker run myapp:${{ github.sha }} npm test
      - name: Scan image for vulnerabilities
        run: docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy image myapp:${{ github.sha }}
      - name: Push to registry
        run: |
          echo ${{ secrets.DOCKER_PASSWORD }} | docker login -u ${{ secrets.DOCKER_USERNAME }} --password-stdin
          docker push myapp:${{ github.sha }}
  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - name: Deploy to production
        run: docker pull myapp:${{ github.sha }} && docker run -d myapp:${{ github.sha }}
```

Key steps: build, test, scan, push, deploy.

### 41. What is the Container Runtime Interface (CRI)?

CRI is a Kubernetes interface for managing containers and container images. It abstracts container runtime details from Kubernetes orchestration. Benefits:

- **Multiple runtimes**: Support Docker, containerd, CRI-O, etc.
- **Decoupling**: Kubernetes doesn't depend on Docker specifically
- **Standardization**: Consistent interface for all runtimes

CRI defines two services:
- **ImageService**: Manages images (pull, inspect, remove)
- **RuntimeService**: Manages containers (create, start, stop, logs)

Kubernetes communicates with runtimes via CRI, enabling flexibility in runtime choice.

### 42. How do you limit container resources?

Use `--cpus`, `-m/--memory`, and other flags:

```bash
# CPU limits
docker run --cpus=0.5 myimage              # Max 0.5 CPU cores
docker run --cpus=2 myimage                # Max 2 CPU cores
docker run --cpu-shares=1024 myimage       # Relative weight (default 1024)

# Memory limits
docker run -m 512m myimage                 # Max 512MB
docker run -m 2g myimage                   # Max 2GB
docker run -m 1g --memory-swap 2g myimage # Limit memory+swap

# Block I/O
docker run --blkio-weight=300 myimage      # Relative I/O priority

# PID limit
docker run --pids-limit 100 myimage        # Max 100 processes
```

In docker-compose:
```yaml
services:
  web:
    deploy:
      resources:
        limits:
          cpus: '0.5'
          memory: 512M
        reservations:
          cpus: '0.25'
          memory: 256M
```

### 43. What is the docker0 bridge?

`docker0` is the default Docker bridge network interface on the host. When Docker daemon starts:

1. A bridge interface `docker0` is created (usually 172.17.0.1)
2. Containers connected to the bridge are assigned IPs from this range
3. The host routes traffic between containers via the bridge
4. Containers can reach the host via the bridge gateway

View bridge configuration:
```bash
ip link show docker0
brctl show
```

Containers on the same bridge can communicate by IP or container name (with Docker's embedded DNS). The default bridge network has limitations (no automatic DNS resolution for container names); named bridge networks provide better isolation and service discovery.

### 44. Explain container orchestration and its benefits

Container orchestration automates deployment, management, and scaling of containers across clusters. Orchestrators (Kubernetes, Docker Swarm, Nomad):

**Key responsibilities**:
- **Scheduling**: Place containers on appropriate nodes
- **Scaling**: Automatically adjust replica counts
- **Health management**: Restart failed containers
- **Rolling updates**: Update applications without downtime
- **Load balancing**: Distribute traffic across replicas
- **Resource optimization**: Maximize cluster utilization

**Benefits**:
- **High availability**: Automatic failover
- **Scalability**: Handle traffic spikes
- **Efficiency**: Better resource usage
- **Automation**: Reduces manual ops
- **Self-healing**: Auto-restart failed containers

Kubernetes is industry-standard; Docker Swarm is simpler for smaller deployments.

### 45. How do you handle secrets in Docker?

**Never hardcode secrets**. Best practices:

1. **Docker secrets** (Swarm):
```bash
echo "db_password" | docker secret create db_pass -
docker service create --secret db_pass myapp
```

2. **Environment variables** (at runtime, not in Dockerfile):
```bash
docker run -e DB_PASSWORD=secret myimage
```

3. **Docker .env files**:
```bash
docker run --env-file .env myimage
```

4. **External vaults** (HashiCorp Vault, AWS Secrets Manager):
```dockerfile
FROM myimage
RUN apt-get install -y curl
CMD ["sh", "-c", "curl https://vault.example.com/api/v1/secret/db_password | jq .data.value > /run/secrets/db_password && exec /app/start.sh"]
```

5. **Bind mount secrets** (production):
```bash
docker run -v /path/to/secrets:/run/secrets myimage
```

Never commit `.env` files or secrets to version control.

### 46. What is a Docker socket and what are the security implications?

The Docker socket (`/var/run/docker.sock`) is a Unix socket that enables communication with the Docker daemon. It allows clients to create/manage containers, images, and volumes.

**Security implications**:

1. **Full daemon access**: Any process with socket access can control Docker completely
2. **Container escape**: A compromised container with socket access can escape to the host
3. **Host compromise**: An attacker can create privileged containers with host access

**Example attack**:
```bash
# Inside a container with socket mounted
docker run -v /var/run/docker.sock:/var/run/docker.sock docker
# Now attacker can run privileged containers
docker run -v /:/host docker
# Has full host access
```

**Mitigation**:
- Never mount Docker socket in untrusted containers
- Restrict socket permissions (only root/docker group)
- Use rootless Docker mode
- Implement resource limits and security policies
- Run containers with minimal privileges

### 47. How do you optimize Docker image size?

Strategies to reduce image size:

1. **Use minimal base images**:
```dockerfile
FROM alpine:latest        # ~5MB (better than ubuntu:20MB)
FROM scratch              # No base image (~0MB)
```

2. **Multi-stage builds**:
```dockerfile
FROM golang:1.20 AS builder
RUN go build -o app .
FROM alpine
COPY --from=builder /app /app
```

3. **Combine RUN commands** (reduces layers):
```dockerfile
# Bad: 3 layers
RUN apt-get update
RUN apt-get install -y curl
RUN apt-get clean

# Good: 1 layer
RUN apt-get update && apt-get install -y curl && apt-get clean
```

4. **Cleanup after installation**:
```dockerfile
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
```

5. **Use .dockerignore** to exclude unnecessary files

6. **Minimize layers** by grouping instructions

7. **Use specific base image tags**:
```dockerfile
FROM node:18-alpine  # Specific, minimal
FROM node            # Latest, larger
```

Typical reductions: 50% smaller images with optimizations.

### 48. Explain the Docker build cache and cache busting

Docker caches each layer during build. To reuse cached layers:

1. Instruction and context must match exactly
2. Layers are only reused if all previous layers match

**Cache-busting strategies**:

1. **File changes**: Modifying `COPY`/`ADD` files busts cache
2. **Timestamp changes**: Re-running `RUN` commands always rebuilds
3. **Environment changes**: Different build args bust cache
4. **--no-cache flag**: Rebuild all layers from scratch

```bash
docker build -t myimage .
docker build --no-cache -t myimage .          # Force rebuild
docker build --build-arg CACHE_BUST=$(date +%s) -t myimage .
```

**Optimization**: Order Dockerfile instructions from least-changing to most-changing:
```dockerfile
FROM ubuntu
RUN apt-get update && apt-get install -y python3  # Rarely changes
COPY requirements.txt .                           # Changes occasionally
RUN pip install -r requirements.txt
COPY app.py .                                     # Changes frequently
```

### 49. What is the difference between Docker Swarm and Kubernetes?

| Feature | Docker Swarm | Kubernetes |
|---------|--------------|-----------|
| **Complexity** | Simple, easy to use | Complex, steep learning curve |
| **Setup time** | Minutes | Hours/days |
| **Cluster size** | Good for small clusters | Scales to thousands of nodes |
| **Features** | Basic orchestration | Comprehensive features |
| **Networking** | Built-in, basic | Advanced (CNI plugins) |
| **Storage** | Limited | Extensive (PV, PVC, storage classes) |
| **Community** | Smaller | Larger, industry standard |
| **Production use** | Small-medium deployments | Enterprise deployments |

**Use Swarm for**: Small teams, simple applications, fast setup
**Use Kubernetes for**: Large scale, complex requirements, enterprise needs

### 50. How do you troubleshoot a failing container?

**Step-by-step troubleshooting**:

1. **Check container status**:
```bash
docker ps -a                          # See if container is running
docker inspect container_id           # Detailed configuration
```

2. **View logs**:
```bash
docker logs container_id              # Full logs
docker logs -f container_id           # Follow logs (tail -f)
docker logs --tail 50 container_id    # Last 50 lines
```

3. **Execute commands inside**:
```bash
docker exec -it container_id /bin/bash    # Interactive shell
docker exec container_id ps aux            # Check processes
```

4. **Check resource constraints**:
```bash
docker stats container_id              # CPU, memory, network usage
docker inspect container_id | grep -i memory  # Memory limits
```

5. **Verify networking**:
```bash
docker exec container_id ping google.com    # Test connectivity
docker network inspect bridge               # Check network configuration
docker inspect container_id | grep -i ipaddress
```

6. **Check mount points**:
```bash
docker inspect container_id | grep -i mount
df -h                                       # Disk space
```

7. **Restart and debug**:
```bash
docker restart container_id
docker run -it image_id /bin/bash          # Run interactively to debug
```

8. **Health checks**:
```bash
docker inspect container_id | grep -i health
```

---

## Tips for Docker Interviews

1. **Understand the fundamentals deeply**: Master namespaces, cgroups, layers, and copy-on-write. These concepts appear across all difficulty levels and show deep understanding.

2. **Know practical differences**: Be ready to explain when to use volumes vs bind mounts, CMD vs ENTRYPOINT, or stop vs kill with real-world examples. Interviewers appreciate practical knowledge.

3. **Security is crucial**: Always mention security considerations (non-root users, resource limits, scanning images, secrets management). In production, security is paramount.

4. **Discuss trade-offs**: There's rarely one "correct" answer. Discuss trade-offs between simplicity and features (Swarm vs Kubernetes), performance and security (rootless Docker), or image size vs development convenience.

5. **Prepare detailed examples**: Have concrete code examples ready for Dockerfile best practices, docker-compose configurations, and networking setups. Show you've actually built and deployed containers.

6. **Stay current**: Docker and container ecosystems evolve. Be aware of container runtimes (containerd, CRI-O), newer features (rootless Docker, Docker BuildKit), and how they compare to alternatives.

7. **Think about edge cases**: Discuss what happens when containers fail, how to handle state, data persistence, and debugging. Show you've troubleshot real issues in production environments.

---

**Last updated**: March 21, 2026

For more Docker learning, explore the [Docker Getting Started](https://docs.docker.com/get-started/) guide and the [CloudCaptain Docker documentation](/docs/tools/docker).
