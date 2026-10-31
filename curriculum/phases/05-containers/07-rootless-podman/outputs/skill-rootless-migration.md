---
name: rootless-migration
description: Move a Docker host or CI runner to rootless Podman or rootless Docker and fix the five usual breakages (volume ownership, ports below 1024, docker.sock consumers, containers dying at logout, missing subordinate id ranges) by reading the user namespace map instead of guessing
version: 1.0.0
phase: containers
lesson: containers/rootless-podman
tags: [containers, podman, docker, rootless, user-namespaces, security, migration, systemd]
---

# Rootless migration

Use this when a team wants to stop running containers through a root daemon (shared CI runners, developer workstations, single-node app hosts), or when a rootless setup already exists and "worked on Friday, broke on Monday". Every step ends in a command whose output goes into the report.

## 1. Preflight the account

```bash
U=$(id -un); echo "$U uid=$(id -u)"
grep -E "^($U|$(id -u)):" /etc/subuid /etc/subgid || echo "NO RANGES"
command -v newuidmap newgidmap || echo "NO HELPERS"
sysctl kernel.unprivileged_userns_clone kernel.apparmor_restrict_unprivileged_userns 2>/dev/null
unshare -Ur id -u                                     # 0 = user namespaces work for this account
stat -fc %T /sys/fs/cgroup                            # cgroup2fs; anything else means no rootless resource limits
cat /sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers 2>/dev/null   # want: cpu memory pids
python3 userns.py map                                 # or: podman info --format '{{json .Host.IDMappings}}'
```

| Finding | Fix |
|---|---|
| NO RANGES | `sudo usermod --add-subuids START-END --add-subgids START-END $U` with a block nobody else uses (`userns map` suggests one), then `podman system migrate` |
| NO HELPERS | install `uidmap` (Debian family) or `shadow-utils` (RHEL family); `newuidmap` and `newgidmap` must be setuid root |
| `unshare -Ur` fails with EPERM | `kernel.unprivileged_userns_clone=1` on Debian-style kernels; `kernel.apparmor_restrict_unprivileged_userns=0` on recent Ubuntu, in `/etc/sysctl.d/`; or run the engine under an AppArmor profile that allows user namespaces |
| cgroup v1, or no delegated controllers | boot with cgroup v2; add `Delegate=cpu cpuset io memory pids` under `[Service]` in `/etc/systemd/system/user@.service.d/delegate.conf`, then `systemctl daemon-reload` |

## 2. Inventory what the current setup relies on

```bash
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Ports}}'
for c in $(docker ps -aq); do
  docker inspect -f '{{.Name}} privileged={{.HostConfig.Privileged}} user={{.Config.User}} caps={{.HostConfig.CapAdd}} net={{.HostConfig.NetworkMode}} restart={{.HostConfig.RestartPolicy.Name}} mounts={{range .Mounts}}{{.Source}}:{{.Destination}} {{end}}' "$c"
done
grep -rl docker.sock /etc/systemd/system ~/.config /opt 2>/dev/null   # who talks to the daemon socket
```

Flag: published ports below 1024, bind mounts and their owners on the host (`ls -ln`), `--privileged` or `CapAdd`, `docker.sock` mounts, `restart: always` (needs systemd units in rootless), `--network host`, devices, images whose `USER` is not root.

## 3. The five breakages

| Symptom | Mechanism | Prove it | Fix |
|---|---|---|---|
| Volume files owned by a six-digit uid; `rm -r` fails on subdirectories | container uid N is host uid `subuid_start + N - 1` | `podman unshare cat /proc/self/uid_map`; `python3 userns.py translate --uid-map FILE N` | `-v dir:/data:U`, or `--userns=keep-id`, or `podman unshare chown -R N:N dir`; never `sudo chown` to a number you have not translated |
| `rootlessport cannot expose privileged port 80` | the host-side listener is pasta or slirp4netns running as you, with no `CAP_NET_BIND_SERVICE` | `sysctl net.ipv4.ip_unprivileged_port_start` | `net.ipv4.ip_unprivileged_port_start=80` in `/etc/sysctl.d/`, or publish 8080 and let a system proxy or an `nft redirect` rule own 80 |
| `Cannot connect to the Docker daemon at unix:///var/run/docker.sock` | there is no root daemon any more | `ls -l /var/run/docker.sock "$XDG_RUNTIME_DIR/podman/podman.sock"` | `systemctl --user enable --now podman.socket`; `export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/podman/podman.sock`; mount that socket, not `/var/run/docker.sock`, into tools that need the API |
| containers vanish at logout; `restart: always` does nothing | no user manager after the session ends, and no daemon to restart anything | `loginctl show-user $U -p Linger`; `systemctl --user status` | `loginctl enable-linger $U`; Quadlet `.container` units in `~/.config/containers/systemd/`, then `systemctl --user daemon-reload && systemctl --user start NAME` |
| `no subuid ranges found`, or `lchown ... invalid argument` during pull | no grants, or the image has file owners beyond your range | `python3 userns.py map`; the pull error names the id | `usermod --add-subuids --add-subgids` plus `podman system migrate`; for images with large ids map a bigger range or use `--userns=auto` |

## 4. Switch

```bash
sudo apt-get install -y podman uidmap             # or: sudo dnf install -y podman
alias docker=podman                                # or install podman-docker for a system-wide shim
systemctl --user enable --now podman.socket
export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/podman/podman.sock
docker compose up -d                               # the compose client talks to the socket unchanged; podman compose also works
loginctl enable-linger "$(id -un)"
```

Services that must outlive the session become Quadlet units instead of `restart: always`:

```ini
# ~/.config/containers/systemd/web.container
[Container]
Image=docker.io/library/nginx:alpine
PublishPort=8080:80
Volume=%h/web/data:/data:U

[Service]
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user start web && systemctl --user status web
```

If the team must keep the `docker` CLI and daemon model, rootless Docker is the same mechanism with RootlessKit owning the user namespace:

```bash
sudo apt-get install -y docker-ce-rootless-extras uidmap
dockerd-rootless-setuptool.sh install
export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/docker.sock
docker info -f '{{.SecurityOptions}}'             # must contain name=rootless
```

## 5. Verify

```bash
podman run --rm alpine:3.22 cat /proc/self/uid_map       # two lines, the first "0 <your uid> 1"; "0 0 4294967295" means you are still rootful
podman run --rm -v "$PWD/data:/data:U" alpine:3.22 sh -c 'id -u; touch /data/ok' && ls -ln data
podman run --rm -d --name probe -p 8080:80 docker.io/library/nginx:alpine && curl -sI localhost:8080 | head -n 1; podman rm -f probe
podman top -l huser user                                  # host uid next to container uid for each process
systemctl --user is-active podman.socket; loginctl show-user "$(id -un)" -p Linger
```

## 6. Report

State: the account and its `uid_map` lines; kernel, sysctl and cgroup findings; every published port below 1024 and how it was handled; every volume and its ownership strategy (`:U`, `keep-id` or a one-time `podman unshare chown`); how `docker.sock` consumers were repointed; how containers survive logout. Say what did not change: the kernel is shared, so a kernel exploit still escapes, and anything that runs as this account (a leaked socket, a setuid binary in a volume) runs with this account's rights, not root's. Say explicitly that "member of the docker group equals root" is gone.
