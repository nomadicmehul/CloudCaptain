---
name: image-layer-audit
description: Turn `docker history` plus the layer_audit report into a concrete plan to shrink an image and remove leaked files
phase: containers
lesson: containers/image-layers
---

You are reviewing a container image for size and for leaked files. I will paste:

1. The output of `docker history --no-trunc --format '{{.Size}}\t{{.CreatedBy}}' IMAGE`.
2. The output of `python3 layer_audit.py image.tar` (per-layer sizes, deleted and overwritten files, wasted bytes).
3. Optionally the Dockerfile.

Produce exactly these sections:

**Where the bytes are**
A table of the five largest layers: size, the Dockerfile instruction that created it, and one line on whether that size is expected for what the instruction does.

**Wasted bytes**
List every file reported as deleted or overwritten in a later layer, with its size and the two instructions involved. Flag anything that looks like a secret, a package cache (`/var/cache/apt`, `~/.cache/pip`, `node_modules/.cache`), a build toolchain, or source files that a later stage should not ship.

**Rewrite plan**
Ordered Dockerfile changes, most bytes saved first. For each: the instruction(s) to change, the technique (multi-stage build, `--mount=type=cache`, `--mount=type=secret`, combining RUN steps with cleanup in the same layer, `.dockerignore`, a smaller base image), and the estimated saving taken from the numbers above. Do not invent numbers.

**Secrets**
If any deleted file could be a credential, say so plainly in one sentence and state that it must be rotated, because every pulled copy of the image still contains it.

Keep the whole answer under 350 words. Only reason from the pasted data.
