---
name: lab-doctor
description: Diagnose whether this machine can run a CloudCaptain lesson (or any container/Kubernetes/IaC tutorial): tools, versions, Linux features, and the exact install commands for this OS
version: 1.0.0
phase: setup
lesson: setup/lab-doctor
tags: [setup, docker, kubernetes, kind, opentofu, troubleshooting]
---

# Lab doctor

Use this before starting a lesson, when a tutorial fails for an environmental reason ("command not found", "permission denied", "cgroup", "connection refused to the Docker daemon"), or when asked "is my machine ready?".

## 1. Run the doctor if it is available

```bash
python3 curriculum/phases/00-setup/01-lab-doctor/code/main.py check
python3 curriculum/phases/00-setup/01-lab-doctor/code/main.py plan
```

If the file is not present, probe by hand in this order and record what you find:

```bash
git --version; python3 --version; node --version
docker --version && docker info --format '{{.ServerVersion}} {{.OSType}} cgroup={{.CgroupVersion}}'
kubectl version --client; kind --version || k3d version
tofu version || terraform version
uname -sr; [ -f /sys/fs/cgroup/cgroup.controllers ] && echo "cgroup v2" || echo "no cgroup v2"
```

## 2. Classify each finding

| Symptom | Layer | Fix |
|---|---|---|
| `command not found` | tool missing | the `plan` output, or the brew/apt/dnf line for the tool |
| version below the lesson's minimum | tool old | upgrade with the same package manager; note Docker Desktop updates separately from the CLI |
| `permission denied … docker.sock` | daemon access | `sudo usermod -aG docker $USER` and re-login, or start Docker Desktop |
| `Cannot connect to the Docker daemon` | daemon not running | `sudo systemctl start docker` / open Docker Desktop |
| `unshare: Operation not permitted`, no `/sys/fs/cgroup/cgroup.controllers` | not a Linux kernel you control | macOS: `limactl start`; Windows: WSL 2; or run the step inside `docker run --privileged -it ubuntu` |
| `kind create cluster` hangs | Docker resources | give Docker Desktop at least 4 GB RAM; `kind delete cluster` and retry |

## 3. Report

Three lines: what is ready, what is not with the exact command to fix it, and whether the current lesson can proceed (Build It needs Python; Use It needs the tool named in the lesson's Metadata table). Never suggest `--privileged`, `sudo pip`, or disabling SELinux/AppArmor as a fix for a tooling gap.
