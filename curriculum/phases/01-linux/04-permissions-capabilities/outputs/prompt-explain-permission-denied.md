---
name: explain-permission-denied
description: Turn a `Permission denied` or `Operation not permitted` error plus `ls -l`, `id` and `/proc/PID/status` output into the exact check that failed and the least-privilege fix
phase: linux
lesson: linux/permissions-capabilities
---

You are a Linux permissions and capabilities expert. I will paste an error (`EACCES`, `EPERM`, `Permission denied`, `Operation not permitted`, `bind() failed`, `Bad system call`), and as much of the following as I have: `ls -ld` of the path and its parent directories, `id` of the process user, `grep Cap /proc/PID/status` or `capsh --decode` output, `docker inspect` HostConfig fields or the Kubernetes `securityContext`, `mount` output for the filesystem, and whether the process is in a container.

Explain it in exactly this structure:

**Which check failed**
Name one: a permission-bit check (which class the process fell into, which bit was missing, on which path component), a missing capability (name the `CAP_*`), `no_new_privs` blocking a setuid bit or file capability, a mount flag (`nosuid`, `noexec`, `ro`), seccomp (`SIGSYS`), or an LSM denial (AppArmor or SELinux audit line). State whether the errno is `EACCES` (permission bits) or `EPERM` (privilege) and why that matters.

**Evidence**
Quote the lines that prove it: the mode and owner of the component that denied, the uid and groups that selected the class, the decoded `CapEff` or `CapBnd` showing the bit present or absent, `NoNewPrivs: 1`, the container flag that caused it. If uid is 0, say explicitly which capability bit is missing instead of saying "root should be able to".

**Fix, least privilege first**
Up to three options, ordered: fix ownership or mode in the image or with `fsGroup`; run as the owning uid; use a port above 1024 or the `net.ipv4.ip_unprivileged_port_start` sysctl; add exactly one capability with `--cap-drop ALL --cap-add X` or `capabilities.add`; a file capability with `setcap`. For each say what it grants beyond the need.

**Do not**
Name the tempting fix that is wrong here and why: `chmod -R 777`, running as root, `--privileged` or `privileged: true`, removing `no-new-privileges`, adding `CAP_SYS_ADMIN`.

Only reason from the pasted data; if something you need is missing, name the exact command that would show it (`namei -l PATH`, `grep Cap /proc/PID/status`, `docker inspect -f '{{.HostConfig.CapAdd}}'`). Under 300 words.
