# Containers

*Write a container runtime in a few hundred lines, then master Docker, Podman, OCI images and Compose.*

Most container trouble comes from treating a container as a small virtual machine. It is not. It is a Linux process with a restricted view of the machine (namespaces), a resource budget (cgroups) and a different idea of where `/` points (a root filesystem). Once you have built that yourself, every Docker flag, Kubernetes pod spec and "works on my machine" bug maps onto something you have typed.

## How this phase is built

Each lesson follows the same arc: a concrete production problem, the concept with a diagram, a from-scratch implementation in Python and Bash, the same job with Docker or Podman, and an artifact (a prompt, a skill or an agent definition) you keep.

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| Process Isolation | Build | `unshare` + `fork` + `chroot` + a cgroup, in Python | `docker run --hostname --memory --pids-limit`, `lsns`, `nsenter` |

More lessons in this phase are listed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md): image layers and the OCI format, building images without a Dockerfile, registries and content addressing, networking between containers, Compose, rootless containers with Podman, and runtime security.

## Reference layer

The long-form guides, cheat sheet and interview questions for this phase live under [Docker](https://cloudcaptain.io/docs/tools/docker/) and [Podman](https://cloudcaptain.io/docs/tools/podman/). Lessons link into them; the cheat sheet is the thing to keep open while you work.
