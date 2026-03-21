---
title: "Docker Commands"
description: "Essential Docker command reference"
---

# Docker Commands Cheatsheet

## Container Lifecycle

```bash
# Run a container
docker run -d --name myapp -p 8080:80 nginx

# List running containers
docker ps

# List all containers
docker ps -a

# Stop a container
docker stop myapp

# Remove a container
docker rm myapp

# View logs
docker logs -f myapp

# Execute command in container
docker exec -it myapp bash
```

## Image Management

```bash
# Build an image
docker build -t myapp:latest .

# List images
docker images

# Pull an image
docker pull nginx:alpine

# Push an image
docker push myrepo/myapp:latest

# Remove an image
docker rmi myapp:latest

# Tag an image
docker tag myapp:latest myrepo/myapp:v1.0
```

## Docker Compose

```bash
# Start services
docker compose up -d

# Stop services
docker compose down

# View logs
docker compose logs -f

# Scale a service
docker compose up -d --scale web=3

# Rebuild services
docker compose up -d --build
```

## Networking

```bash
# List networks
docker network ls

# Create a network
docker network create mynet

# Connect container to network
docker network connect mynet myapp

# Inspect network
docker network inspect mynet
```

## Volumes

```bash
# Create a volume
docker volume create mydata

# List volumes
docker volume ls

# Mount a volume
docker run -v mydata:/data myapp

# Bind mount
docker run -v $(pwd):/app myapp
```

## System Management

```bash
# System info
docker system info

# Disk usage
docker system df

# Clean up unused resources
docker system prune -a

# View real-time resource stats
docker stats
```

---

## Docker Swarm

### Initialize & Join

```bash
# Initialize Docker Swarm
docker swarm init

# Join cluster as manager or worker
docker swarm join --token SWMTKN-1-3pu6hszjas19xyp7ghgosyx9k8atbfcr8p2is99znpy26u2lkl-1awxwuwd3z9j1z3puu7rcgdbx <manager/worker>:2377

# List nodes in cluster
docker node ls
```

### Stack Management

```bash
# List running applications
docker stack ls

# Deploy an application from Compose file
docker stack deploy -c <composefile> <STACK_NAME>

# List services for an application
docker stack services <appname>

# List running containers for an application
docker stack ps <appname>

# Remove application
docker stack rm <STACK_NAME>
```

### Service Management

```bash
# List services
docker service ls

# List tasks of a service
docker service ps <service_name>

# View service logs
docker service logs <service_name>

# Remove a service
docker service rm <service_name>
```

### Configuration & Secrets

```bash
# Create a secret
docker secret create <SECRET_NAME> <SECRET_PATH>

# Create a config
docker config create <CONFIG_NAME> <CONFIG_FILE_PATH>
```

### Cleanup Commands

```bash
# Remove unused containers, images, networks, volumes
docker system prune

# Remove unused volumes only
docker volume prune
```
