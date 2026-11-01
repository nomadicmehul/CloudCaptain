---
name: reproducible-image
description: Make a container image build reproducible (same inputs, same image ID and manifest digest) and prove it by building twice and diffing digests, layer by layer
version: 1.0.0
phase: containers
lesson: containers/image-build
tags: [containers, docker, oci, buildkit, reproducible-builds, supply-chain]
---

# Reproducible image

Use this when a team wants "the same commit gives the same image", when a signature or SBOM must apply to rebuilt images, or when two CI runs of one commit produce different image IDs and nobody knows why.

## 1. Measure before changing anything

```bash
docker build -t probe:a . && docker build --no-cache -t probe:b .
docker image inspect probe:a probe:b --format '{{.Id}} {{json .RootFS.Layers}}'
```

Same `RootFS.Layers` but different IDs means only the config differs (timestamps, labels with dates). Different `diff_ids` means layer content or metadata differs; find the first layer that differs and work on that instruction.

## 2. Pin every input

- Base by digest: `FROM alpine:3.22@sha256:…` (`crane digest alpine:3.22` gives it). A tag is a moving pointer.
- Package versions pinned in the install step; lockfiles copied before the install.
- No `apt-get update` without a snapshot source, no `curl | sh`, no `latest`.

## 3. Fix time and ownership

```bash
export SOURCE_DATE_EPOCH=$(git log -1 --pretty=%ct)
docker buildx build --output type=oci,dest=img.tar,rewrite-timestamp=true -t app:repro .
```

`rewrite-timestamp=true` rewrites every tar entry's mtime to `SOURCE_DATE_EPOCH`, which removes the most common source of drift. Remove instructions that write the current time into files or labels (`LABEL build-date=…`, generated version files) or derive them from the commit instead. Keep file ownership deterministic: avoid `COPY --chown` with names resolved at build time, and avoid tools that write user names into archives.

## 4. Separate metadata from filesystem

Put `ENV`, `LABEL`, `ENTRYPOINT`, `CMD`, `USER`, `EXPOSE` after the last filesystem instruction so a metadata change does not invalidate layer cache, and check `docker history` shows them as empty-layer rows.

## 5. Prove it

```bash
SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH docker buildx build --output type=oci,dest=a.tar,rewrite-timestamp=true .
SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH docker buildx build --no-cache --output type=oci,dest=b.tar,rewrite-timestamp=true .
cmp a.tar b.tar && echo byte-identical
```

If `cmp` fails, compare the pieces: `tar -xf a.tar -C a && tar -xf b.tar -C b`, then `diff <(jq -S . a/index.json) <(jq -S . b/index.json)` and work down to the first layer blob whose digest differs. Unpack that layer and `diff -r` the two trees; the differing file names the instruction to fix. `skopeo inspect --raw oci:a` and `crane manifest` print the manifests for the same comparison without unpacking.

## 6. Report

State: the inputs pinned (base digest, lockfiles), the epoch used and where it comes from, the build command, the two image IDs and manifest digests that now match, and anything that still differs with its cause. Say explicitly that reproducibility is verified by equal digests, not by inspection, and that a signature over the manifest digest now covers every faithful rebuild.
