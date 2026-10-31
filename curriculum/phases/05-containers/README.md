# Containers

*Write a container runtime in a few hundred lines, then master Docker, Podman, OCI images and Compose.*

Most container trouble comes from treating a container as a small virtual machine. It is not. It is a Linux process with a restricted view of the machine (namespaces), a resource budget (cgroups) and a different idea of where `/` points (a root filesystem). Once you have built that yourself, every Docker flag, Kubernetes pod spec and "works on my machine" bug maps onto something you have typed.

## How this phase is built

Each lesson follows the same arc: a concrete production problem, the concept with a diagram, a from-scratch implementation in Python and Bash, the same job with Docker or Podman, and an artifact (a prompt, a skill or an agent definition) you keep.

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| Process Isolation | Build | `unshare` + `fork` + `chroot` + a cgroup, in Python | `docker run --hostname --memory --pids-limit`, `lsns`, `nsenter` |
| Image Layers | Learn | a `docker save` auditor that finds deleted and overwritten bytes | `docker history`, `docker image inspect`, build-cache ordering, secret mounts |
| Registry Pull | Build | an OCI registry client: token flow, index → manifest → blobs, digest verification, safe rootfs extraction | `docker pull`, `docker manifest inspect`, `crane`, `skopeo`, `curl` |
| Image Build | Build | `mkimage`: directories → deterministic layers, two digests each, chain IDs, config, manifest, OCI layout + `docker load` archive, `verify` | `docker build`/`save`/`history`, BuildKit `SOURCE_DATE_EPOCH` + `rewrite-timestamp`, `crane append`, `buildah`, `skopeo` |
| Container Networking | Build | `ccnet`: a network namespace, a veth pair, a bridge and the NAT rules per container, with an IPAM and a plumbing checker in Python | `docker network inspect`, `docker run -p`, `nsenter -n ip addr`, `nft` / `iptables -t nat`, CNI plugins |
| Compose | Build | `minicompose`: a YAML-subset parser, a service dependency graph, health-gated `docker run` ordering, `up` / `ps` / `down` | `docker compose up --wait`, `depends_on.condition`, healthchecks, `compose config` (Docker Swarm is in maintenance mode) |
| Rootless Podman | Build | `userns`: subuid mappings, `uid_map` rendering and a user-namespace launcher through `unshare(2)` | `podman unshare`, `--userns=keep-id`, rootless Docker, Kubernetes user namespaces |
| Runtime Security | Build | `hardencheck`: seccomp profile analysis and generation from an strace log, hardening scores for `docker inspect` output and Pod manifests | `--security-opt seccomp`, `--cap-drop ALL --read-only`, Pod Security Admission (restricted), Falco |

Further lessons for this phase are proposed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md).

## Reference layer

The long-form guides, cheat sheet and interview questions for this phase live under [Docker](https://cloudcaptain.io/docs/tools/docker/) and [Podman](https://cloudcaptain.io/docs/tools/podman/). Lessons link into them; the cheat sheet is the thing to keep open while you work.
