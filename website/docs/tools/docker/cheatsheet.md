---
title: "Docker Command Cheat Sheet"
sidebar_label: "Cheat Sheet"
description: "Complete Docker CLI reference — containers, images, volumes, networks, compose, and system commands"
sidebar_position: 7
---

# Docker Command Cheat Sheet

Complete reference for Docker CLI commands. Organized by category for easy lookup.

## Container Lifecycle

| Command | Description |
|---|---|
| `docker run [OPTIONS] IMAGE [COMMAND]` | Create and start a new container |
| `docker create IMAGE` | Create a container without starting it |
| `docker start CONTAINER` | Start a stopped container |
| `docker stop CONTAINER` | Stop a running container (graceful) |
| `docker restart CONTAINER` | Stop and restart a container |
| `docker pause CONTAINER` | Pause all processes in container |
| `docker unpause CONTAINER` | Resume paused container |
| `docker kill CONTAINER` | Force stop container (SIGKILL) |
| `docker rm CONTAINER` | Delete a stopped container |
| `docker rm -f CONTAINER` | Force delete a running container |
| `docker wait CONTAINER` | Wait for container to stop, return exit code |

### Common `docker run` Options

| Flag | Description | Example |
|---|---|---|
| `-d` | Detached mode (background) | `docker run -d nginx` |
| `-it` | Interactive + TTY (terminal) | `docker run -it ubuntu bash` |
| `-p` | Port mapping (host:container) | `docker run -p 8080:80 nginx` |
| `--expose` | Expose port (internal) | `docker run --expose 8080 nginx` |
| `-v` | Volume mount (host:container) | `docker run -v /data:/app nginx` |
| `--mount` | Advanced mount (type=bind/volume/tmpfs) | `docker run --mount type=bind,src=/data,dst=/app nginx` |
| `--name` | Assign container name | `docker run --name web nginx` |
| `-e` | Environment variable | `docker run -e KEY=value nginx` |
| `--env-file` | Load env vars from file | `docker run --env-file .env nginx` |
| `--network` | Connect to network | `docker run --network mynet nginx` |
| `--rm` | Auto-remove on exit | `docker run --rm nginx` |
| `--restart` | Restart policy | `docker run --restart=always nginx` |
| `-w` | Working directory in container | `docker run -w /app nginx` |
| `-u` | User (uid:gid or name) | `docker run -u 1000:1000 nginx` |
| `--memory` | Memory limit | `docker run --memory=512m nginx` |
| `--memory-swap` | Total memory + swap limit | `docker run --memory=512m --memory-swap=1g nginx` |
| `--cpus` | CPU limit (fraction) | `docker run --cpus=1.5 nginx` |
| `--cpuset-cpus` | CPU cores allowed | `docker run --cpuset-cpus=0,1 nginx` |
| `--cap-add` | Add Linux capability | `docker run --cap-add=NET_ADMIN nginx` |
| `--cap-drop` | Drop Linux capability | `docker run --cap-drop=ALL nginx` |
| `--security-opt` | Security options | `docker run --security-opt=no-new-privileges nginx` |
| `--label` | Add metadata label | `docker run --label version=1.0 nginx` |
| `--link` | Link to another container (deprecated, use networks) | `docker run --link db:database nginx` |
| `-h` | Container hostname | `docker run -h myhost nginx` |
| `--tmpfs` | Mount tmpfs volume | `docker run --tmpfs /tmp nginx` |
| `--init` | Run init process (PID 1) | `docker run --init nginx` |
| `--user` | Run as user | `docker run --user=nobody nginx` |
| `-a` | Attach stdout/stderr/stdin | `docker run -a stdout -a stderr nginx` |

## Container Inspection

| Command | Description |
|---|---|
| `docker ps` | List running containers |
| `docker ps -a` | List all containers (running + stopped) |
| `docker ps -q` | List container IDs only |
| `docker ps --filter "status=running"` | Filter containers by status |
| `docker logs CONTAINER` | View container logs (stdout/stderr) |
| `docker logs -f CONTAINER` | Follow logs (like `tail -f`) |
| `docker logs --tail 50 CONTAINER` | Show last 50 log lines |
| `docker logs --since 2024-01-01 CONTAINER` | Logs since timestamp |
| `docker logs --timestamps CONTAINER` | Include timestamps in logs |
| `docker inspect CONTAINER` | Get detailed container info (JSON) |
| `docker inspect --format='{{.State.Status}}' CONTAINER` | Format inspect output |
| `docker top CONTAINER` | List running processes in container |
| `docker stats CONTAINER` | Live resource usage (CPU, memory, network) |
| `docker stats --no-stream` | Single snapshot of stats |
| `docker port CONTAINER` | List port mappings |
| `docker diff CONTAINER` | Show filesystem changes since container start |
| `docker export CONTAINER` | Export container filesystem as tarball |
| `docker export CONTAINER > container.tar` | Save container to file |

## Container Interaction

| Command | Description |
|---|---|
| `docker exec -it CONTAINER COMMAND` | Execute command in running container |
| `docker exec CONTAINER ls -la` | Run non-interactive command |
| `docker exec -u root CONTAINER apt-get update` | Execute as specific user |
| `docker exec -w /app CONTAINER npm start` | Execute in specific working directory |
| `docker attach CONTAINER` | Attach to container's stdin/stdout/stderr |
| `docker cp CONTAINER:/path/file.txt .` | Copy file from container to host |
| `docker cp file.txt CONTAINER:/path/` | Copy file from host to container |
| `docker cp CONTAINER:/dir/. ./local-dir/` | Copy directory from container |
| `docker rename OLD_NAME NEW_NAME` | Rename a container |
| `docker update --cpus=2 CONTAINER` | Update container resource limits |
| `docker update --restart=always CONTAINER` | Update restart policy |

## Image Management

| Command | Description |
|---|---|
| `docker build -t IMAGE:TAG .` | Build image from Dockerfile |
| `docker build -t IMAGE:TAG -f path/Dockerfile .` | Build from specific Dockerfile |
| `docker build --build-arg KEY=value -t IMAGE:TAG .` | Pass build arguments |
| `docker build --no-cache -t IMAGE:TAG .` | Build without layer cache |
| `docker pull IMAGE:TAG` | Download image from registry |
| `docker pull gcr.io/project/IMAGE:TAG` | Pull from specific registry |
| `docker push IMAGE:TAG` | Upload image to registry |
| `docker images` | List local images |
| `docker images -q` | List image IDs only |
| `docker images --filter "dangling=true"` | List untagged images |
| `docker image ls -a` | List all images (same as `docker images -a`) |
| `docker rmi IMAGE:TAG` | Delete image |
| `docker rmi -f IMAGE:TAG` | Force delete image |
| `docker tag SOURCE:TAG DEST:TAG` | Create image alias/tag |
| `docker save IMAGE:TAG > image.tar` | Export image as tarball |
| `docker save IMAGE:TAG \| gzip > image.tar.gz` | Export compressed image |
| `docker load < image.tar` | Import image from tarball |
| `docker import image.tar IMAGE:TAG` | Import container/tarball as image |
| `docker history IMAGE:TAG` | Show layer history of image |
| `docker inspect IMAGE:TAG` | Get image metadata (JSON) |
| `docker inspect --format='{{.Config.Env}}' IMAGE:TAG` | Get environment variables |

## Volume Management

| Command | Description |
|---|---|
| `docker volume create VOLUME` | Create a named volume |
| `docker volume create --opt type=tmpfs VOLUME` | Create tmpfs volume |
| `docker volume ls` | List volumes |
| `docker volume inspect VOLUME` | Get volume metadata |
| `docker volume rm VOLUME` | Delete a volume |
| `docker volume rm -f VOLUME` | Force delete volume |
| `docker volume prune` | Remove all unused volumes |
| `docker volume prune --filter "until=24h"` | Remove volumes unused for 24h |

## Network Management

| Command | Description |
|---|---|
| `docker network create NETWORK` | Create a custom network |
| `docker network create --driver overlay NETWORK` | Create overlay network (Swarm) |
| `docker network ls` | List networks |
| `docker network inspect NETWORK` | Get network metadata |
| `docker network connect NETWORK CONTAINER` | Connect container to network |
| `docker network disconnect NETWORK CONTAINER` | Disconnect container from network |
| `docker network rm NETWORK` | Delete a network |
| `docker network prune` | Remove all unused networks |

## Docker Compose

| Command | Description |
|---|---|
| `docker compose up` | Create and start containers (uses docker-compose.yml) |
| `docker compose up -d` | Start containers in background |
| `docker compose up -f docker-compose.prod.yml` | Use specific compose file |
| `docker compose down` | Stop and remove containers |
| `docker compose down -v` | Remove containers and volumes |
| `docker compose down --rmi all` | Remove containers, volumes, and images |
| `docker compose ps` | List containers for compose project |
| `docker compose logs` | View logs from all services |
| `docker compose logs -f SERVICE` | Follow logs for specific service |
| `docker compose exec SERVICE COMMAND` | Execute command in running service |
| `docker compose exec -it SERVICE bash` | Interactive shell in service |
| `docker compose build` | Build images for services |
| `docker compose build --no-cache` | Rebuild without cache |
| `docker compose pull` | Pull service images |
| `docker compose config` | Validate and print compose configuration |
| `docker compose restart` | Restart services |
| `docker compose restart SERVICE` | Restart specific service |
| `docker compose stop` | Stop services (don't remove) |
| `docker compose start` | Start stopped services |
| `docker compose pause` | Pause services |
| `docker compose unpause` | Unpause services |
| `docker compose rm` | Remove stopped containers |
| `docker compose scale SERVICE=NUM` | Scale service to N replicas |
| `docker compose run SERVICE COMMAND` | Run one-off command in service |
| `docker compose run --rm SERVICE bash` | Interactive shell (auto-remove) |

## Docker Swarm

| Command | Description |
|---|---|
| `docker swarm init` | Initialize Swarm mode |
| `docker swarm init --advertise-addr 192.168.1.1` | Init with specific advertise address |
| `docker swarm join --token WORKER_TOKEN IP:2377` | Join as worker node |
| `docker swarm join --token MANAGER_TOKEN IP:2377` | Join as manager node |
| `docker swarm join-token worker` | Get worker join token |
| `docker swarm join-token manager` | Get manager join token |
| `docker swarm leave` | Leave Swarm |
| `docker swarm update --autolock=true` | Enable autolock |
| `docker node ls` | List Swarm nodes |
| `docker node inspect NODE` | Get node metadata |
| `docker node update --availability drain NODE` | Set node unavailable |
| `docker node update --availability active NODE` | Activate node |
| `docker node rm NODE` | Remove node from Swarm |
| `docker service create --name SERVICE IMAGE` | Create service |
| `docker service create --replicas 3 --name SERVICE IMAGE` | Create service with replicas |
| `docker service create -p 8080:80 --name SERVICE IMAGE` | Create service with port mapping |
| `docker service ls` | List services |
| `docker service ps SERVICE` | List service tasks (containers) |
| `docker service inspect SERVICE` | Get service metadata |
| `docker service update --image IMAGE:TAG SERVICE` | Update service image |
| `docker service update --replicas 5 SERVICE` | Scale service |
| `docker service scale SERVICE=5` | Scale service (shorthand) |
| `docker service rm SERVICE` | Remove service |
| `docker service logs SERVICE` | View service logs |
| `docker stack deploy -c docker-compose.yml STACK` | Deploy stack |
| `docker stack ls` | List stacks |
| `docker stack services STACK` | List services in stack |
| `docker stack ps STACK` | List tasks in stack |
| `docker stack rm STACK` | Remove stack |
| `docker secret create SECRET file.txt` | Create secret from file |
| `docker secret create SECRET -` | Create secret from stdin |
| `docker secret ls` | List secrets |
| `docker secret inspect SECRET` | Get secret metadata |
| `docker secret rm SECRET` | Delete secret |

## System & Cleanup

| Command | Description |
|---|---|
| `docker system df` | Show Docker disk usage |
| `docker system prune` | Remove unused containers, images, volumes, networks |
| `docker system prune -a` | Also remove unused images (including tagged) |
| `docker system prune -a --volumes` | Also remove unused volumes |
| `docker system prune --filter "until=72h"` | Remove unused for 72+ hours |
| `docker system info` | System-wide information |
| `docker system events` | Monitor Docker events in real-time |
| `docker version` | Show Docker version |
| `docker info` | Detailed system information |
| `docker login` | Login to Docker Hub |
| `docker login -u USERNAME registry.example.com` | Login to custom registry |
| `docker logout` | Logout from Docker Hub |
| `docker search IMAGE` | Search Docker Hub for images |
| `docker image prune` | Remove dangling images |
| `docker image prune -a` | Remove all unused images |
| `docker container prune` | Remove stopped containers |
| `docker volume prune` | Remove unused volumes |
| `docker network prune` | Remove unused networks |

## Dockerfile Quick Reference

| Instruction | Purpose | Example |
|---|---|---|
| `FROM` | Base image | `FROM ubuntu:20.04` |
| `RUN` | Execute command in layer | `RUN apt-get update && apt-get install -y curl` |
| `CMD` | Default command when container starts | `CMD ["python", "app.py"]` |
| `ENTRYPOINT` | Configure container as executable | `ENTRYPOINT ["node"]` |
| `EXPOSE` | Document exposed ports (informational) | `EXPOSE 8080` |
| `ENV` | Set environment variable | `ENV NODE_ENV=production` |
| `ARG` | Build-time variable | `ARG BUILD_DATE=unknown` |
| `WORKDIR` | Set working directory | `WORKDIR /app` |
| `COPY` | Copy files from host to container | `COPY . /app` |
| `ADD` | Copy/extract files (legacy, use COPY) | `ADD archive.tar.gz /app` |
| `USER` | Set user for subsequent commands | `USER appuser` |
| `VOLUME` | Create mount point | `VOLUME ["/data"]` |
| `LABEL` | Add metadata | `LABEL version="1.0"` |
| `HEALTHCHECK` | Define health check | `HEALTHCHECK CMD curl localhost:8080` |
| `SHELL` | Set default shell | `SHELL ["/bin/bash", "-c"]` |

### Dockerfile Example

```dockerfile
# Use specific version for reproducibility
FROM node:18-alpine

# Set metadata
LABEL maintainer="team@example.com"
LABEL version="1.0"

# Set working directory
WORKDIR /app

# Set environment
ENV NODE_ENV=production
ENV PORT=3000

# Install dependencies layer (cached if package.json unchanged)
COPY package*.json ./
RUN npm ci --only=production

# Copy application code
COPY . .

# Create non-root user for security
RUN addgroup -g 1000 appuser && adduser -D -u 1000 -G appuser appuser
USER appuser

# Document exposed port
EXPOSE 3000

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=40s --retries=3 \
  CMD node healthcheck.js

# Run application
CMD ["node", "server.js"]
```

## Multi-Stage Dockerfile Example

```dockerfile
# Stage 1: Build
FROM node:18-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

# Stage 2: Runtime
FROM node:18-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production
COPY --from=builder /app/dist ./dist
USER node
EXPOSE 3000
CMD ["node", "dist/server.js"]
```

## Useful Aliases & Tips

```bash
# Container aliases
alias dc='docker compose'
alias dcup='docker compose up -d'
alias dcdown='docker compose down'
alias dclogs='docker compose logs -f'
alias dcexec='docker compose exec'

# Image aliases
alias dib='docker image build'
alias dil='docker image ls'
alias dir='docker image rm'

# Useful one-liners

# Remove all stopped containers
docker container prune -f

# Remove all dangling images
docker image prune -f

# Stop all running containers
docker stop $(docker ps -q)

# Remove all containers
docker rm $(docker ps -aq)

# Show container resource usage
docker stats --no-stream

# Get container IP address
docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' CONTAINER

# Execute command in all running containers
docker ps -q | xargs -I {} docker exec {} COMMAND

# Copy file from all containers to host
docker ps -aq | xargs -I {} docker cp {}:/path/file.txt ./file-{}.txt

# Build and push in one command
docker build -t myrepo/image:tag . && docker push myrepo/image:tag

# Run container and remove on exit
docker run --rm -it IMAGE bash

# Get full image ID
docker inspect --format='{{.Id}}' IMAGE:TAG

# Show layers of an image
docker history --no-trunc IMAGE:TAG

# Create container without starting it
docker create --name mycontainer IMAGE

# Export and import for migration
docker export CONTAINER | docker import - mynewimage:tag
```

## Docker Registry Operations

| Command | Description |
|---|---|
| `docker login registry.example.com` | Login to registry |
| `docker tag IMAGE:TAG registry.example.com/image:tag` | Tag for custom registry |
| `docker push registry.example.com/image:tag` | Push to custom registry |
| `docker pull registry.example.com/image:tag` | Pull from custom registry |

## Quick Reference: Common Workflows

### Run a Web Server

```bash
# Run nginx, expose port 80 to 8080
docker run -d -p 8080:80 --name web nginx

# Access at http://localhost:8080
```

### Run a Database

```bash
# Run PostgreSQL with volume and env
docker run -d \
  -e POSTGRES_PASSWORD=secret \
  -v pgdata:/var/lib/postgresql/data \
  -p 5432:5432 \
  --name db \
  postgres:14
```

### Interactive Shell

```bash
# Open bash in Ubuntu container
docker run -it --rm ubuntu bash

# Or in running container
docker exec -it CONTAINER bash
```

### Debug Container

```bash
# View logs
docker logs -f CONTAINER

# Execute debugging command
docker exec CONTAINER ps aux

# Get resource stats
docker stats CONTAINER

# Inspect filesystem changes
docker diff CONTAINER
```

### Development Workflow

```bash
# Build image
docker build -t myapp:dev .

# Run with volume mount (hot reload)
docker run -it --rm \
  -v $(pwd):/app \
  -p 3000:3000 \
  myapp:dev

# Stop with Ctrl+C
```

### Production Deployment

```bash
# Build with version tag
docker build -t myrepo/myapp:1.2.3 .

# Push to registry
docker push myrepo/myapp:1.2.3

# On production server, pull and run
docker pull myrepo/myapp:1.2.3
docker run -d \
  --restart=always \
  -e ENV=production \
  -p 80:3000 \
  myrepo/myapp:1.2.3
```

---

## Notes

- **Order matters**: `docker run OPTIONS IMAGE COMMAND` — options go *before* the image
- **Container vs Image**: Image is the template; container is the running instance
- **Volumes persist**: Data in volumes survives container deletion
- **Networks enable communication**: Containers on same network can reach each other by name
- **Compose is for multi-container apps**: Use it locally and in production
- **Always tag images with versions**: Avoid `latest` in production
- **Use non-root users**: Run containers as non-root for security
- **Limit resources**: Always set memory/CPU limits to prevent resource exhaustion
- **Use `.dockerignore`**: Exclude unnecessary files from build context

