---
title: "Docker Interview Prep"
description: "Common Docker interview questions"
---

# Docker Interview Preparation

Comprehensive interview preparation guide for Docker. Check the main [Docker section](/docs/tools/docker/) for detailed learning resources.

## Key Areas to Study

Review the [Docker documentation](/docs/tools/docker/) and practice hands-on exercises.

## Practice Resources

- Review cheatsheets in the Docker section
- Complete hands-on exercises
- Study real-world architecture patterns
- Practice explaining concepts to others

## Core Concepts

### What is Docker?

Docker is an open platform for developing, shipping, and running applications. Docker enables you to separate your applications from your infrastructure so you can deliver software quickly. With Docker, you can manage your infrastructure in the same ways you manage your applications. By taking advantage of Docker's methodologies for shipping, testing, and deploying code quickly, you can significantly reduce the delay between writing code and running it in production.

### What is Docker Compose?

Compose is a tool for defining and running multi-container Docker applications. With Compose, you use a YAML file to configure your application's services. Then, with a single command, you create and start all the services from your configuration.

### YAML File

YAML stands for "YAML Ain't Markup Language", which is a data serialization language that is often used for writing configuration files. YAML stores data in key-value pairs and uses space indentation.

### Expose vs Publish Commands

**Expose**
- Instruction used in Dockerfile
- Exposes ports within a Docker network
- Documenting instruction at build and runtime
- Example: `EXPOSE 8080`

**Publish**
- Used in docker run command
- Can be used outside a Docker environment
- Maps a host port to a running container port
- `--publish` or `-p` is the command used
- Example: `docker run -d -p 0.0.0.80:80`
