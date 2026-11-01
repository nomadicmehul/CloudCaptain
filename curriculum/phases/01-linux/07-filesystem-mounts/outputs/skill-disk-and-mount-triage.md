---
name: disk-and-mount-triage
description: Diagnose "No space left on device", "Read-only file system", "Permission denied" on an executable, df-versus-du gaps and host mounts a container cannot see, by finding the mount a path resolves through and reading its flags, blocks, inodes and propagation
version: 1.0.0
phase: linux
lesson: linux/filesystem-mounts
tags: [linux, mounts, mountinfo, findmnt, df, inodes, overlayfs, docker, kubernetes, debugging]
---

# Disk and mount triage

Use this when a process reports `ENOSPC` (No space left on device), `EROFS` (Read-only file system), `EACCES` from `execve()` on a file whose mode bits are right, when `df` and `du` disagree, or when a mount made on the host does not appear inside a container or pod.

## 1. Which mount does the path resolve through

```bash
P=/path/that/failed
findmnt -T "$P" -o TARGET,SOURCE,FSTYPE,OPTIONS,PROPAGATION      # or: python3 mountlens.py which "$P"
grep -F " $(findmnt -nT "$P" -o TARGET) " /proc/self/mountinfo      # the raw row; field 4 other than / means a bind mount of that subtree
```

Inside a container: `docker exec CID cat /proc/self/mountinfo > ctr.mountinfo` or `kubectl exec POD -- cat /proc/self/mountinfo > pod.mountinfo`, then `python3 mountlens.py which "$P" --file ctr.mountinfo`. From the host: `nsenter -t "$(docker inspect -f '{{.State.Pid}}' CID)" -m findmnt -T "$P"`.

Record TARGET, SOURCE (with the `[subtree]` for binds), FSTYPE, OPTIONS and PROPAGATION before doing anything else.

## 2. Match the error to the row

| Error | Look at | Cause | Fix at the layer that set it |
|---|---|---|---|
| `EROFS` Read-only file system | `ro` in OPTIONS while the superblock options (field 11) say `rw` | a per-mount flag: `-v src:dst:ro`, `--read-only`, `readOnlyRootFilesystem: true`, `mount -o remount,ro` | drop `:ro`, or write to a volume, `emptyDir` or `--tmpfs` instead; never `chmod` |
| `EROFS` and the superblock says `ro` too | `dmesg \| grep -iE 'remount|I/O error'` | ext4 `errors=remount-ro` after a disk error, or the filesystem was mounted `ro` on purpose | fix or replace the disk first; `fsck` from a rescue system; then remount |
| `EACCES` on exec, mode bits fine | `noexec` in OPTIONS (`nosuid` for setuid binaries) | `--tmpfs` default, hardened `/tmp`, `/var/tmp`, `/dev/shm`, `/home` | run from a mount without `noexec`, or `--tmpfs /tmp:exec`; a `chmod` changes nothing |
| `ENOSPC` with free blocks | `df -i PATH`; `IUSE%` in `mountlens usage` | inodes exhausted by millions of small files | delete or archive the small files; on ext4 the inode count is fixed at `mkfs` time |
| `ENOSPC` inside a container, volume looks fine | `df -h /var/lib/docker` on the host, `docker system df -v` | writes to the container root land in `upperdir` on the host disk, not on any volume | clean `/var/lib/docker` (`docker system prune`), move writes to a volume, set `--storage-opt size=` where supported |
| `df` full, `du` small | `lsof +L1` (deleted but open), `mountlens shadowed` (hidden mounts) | space held by a deleted file, or data under a directory that a later mount covers | truncate through `/proc/PID/fd/N` (File Descriptors lesson) or `umount` the covering mount |
| host mount invisible in the container | PROPAGATION of the volume row is `private` | default `rprivate` for `-v`, `None` for `mountPropagation` | `-v src:dst:rslave`, `mountPropagation: HostToContainer` (needs a new container) |
| file edited on host not seen in the container, or vice versa | the row is `overlay`, not a bind | the edit went to an image layer or another container's `merged` directory | use a volume for shared data; never edit under `/var/lib/docker/overlay2` |

## 3. Blocks and inodes, separately

```bash
df -h "$P"; df -i "$P"                                                      # both, every time
df --output=target,fstype,size,used,pcent,itotal,iused,ipcent "$P"
python3 mountlens.py usage                                                  # warnings at 90% of either resource; --all for binds and pseudo filesystems
find "$(findmnt -nT "$P" -o TARGET)" -xdev -type f -printf '%h\n' | cut -d/ -f1-3 | sort | uniq -c | sort -rn | head   # where the inodes went
```

Say which resource is exhausted. A `WARNING ... inode exhaustion` line is the finding; removing a few large files will not help, removing many small ones will.

## 4. Flags are not permissions

`ls -l` and `chmod` describe the file. `ro`, `noexec`, `nosuid` and `nodev` describe the mount point and win every time. Read field 6 for the mount and field 11 for the filesystem; when they disagree, the flag was set by whoever created that mount point (`-v`, `--tmpfs`, `--read-only`, a pod field, `/etc/fstab`, a remount).

## 5. Propagation

```bash
findmnt -o TARGET,PROPAGATION /                                             # shared on systemd hosts
docker inspect -f '{{json .Mounts}}' CID | python3 -m json.tool             # "Propagation": "rprivate" | "rslave" | "rshared"
kubectl get pod POD -o jsonpath='{range .spec.containers[*].volumeMounts[*]}{.mountPath}{" "}{.mountPropagation}{"\n"}{end}'
```

A row without `shared:` or `master:` tags receives nothing. `rslave` / `HostToContainer` receives host mounts under that path; `rshared` / `Bidirectional` also sends them back and needs a privileged container. Propagation is fixed when the container starts.

## 6. Containers and pods

```bash
docker inspect -f '{{json .GraphDriver.Data}}' CID | python3 -m json.tool   # LowerDir (image layers + init layer), UpperDir (where writes go)
docker system df -v                                                         # whose writable layer is growing
kubectl describe pod POD | sed -n '/Mounts:/,/Conditions:/p'
kubectl exec POD -- cat /proc/self/mountinfo > pod.mountinfo && python3 mountlens.py tree --file pod.mountinfo
```

`emptyDir` lives on the node under `/var/lib/kubelet/pods/UID/volumes/`; its `sizeLimit` evicts the pod rather than returning `ENOSPC`. A PVC is the CSI driver's mount on the node bound into the container, so `df` inside the pod reports that filesystem, inodes included.

## 7. Report

State: the failing path and the mount it resolves through (target, source with bind subtree, fstype); the flag or resource behind the error (`ro`, `noexec`, inodes, blocks, propagation, a shadowed mount); where that flag was set (`-v`, `--tmpfs`, `--read-only`, pod field, fstab, remount after error); the fix at that layer; and what you changed. Do not report `chmod`, deleting a few large files, or a restart as the fix unless the row says so.
