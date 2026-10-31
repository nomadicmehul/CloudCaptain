---
name: registry-triage
description: Diagnose ImagePullBackOff, ErrImagePull, "manifest unknown", 401/403/429 and "exec format error" by walking the registry protocol by hand with crane, skopeo or curl
version: 1.0.0
phase: containers
lesson: containers/registry-pull
tags: [docker, kubernetes, registry, oci, debugging, supply-chain]
---

# Registry triage

Use this when a container cannot start because its image cannot be pulled, or when a pulled image is "wrong" (architecture, tag drift, unexpected digest).

## 1. Get the exact error, not the summary

```bash
kubectl describe pod <pod> | sed -n '/Events/,$p'          # look for the Failed/BackOff line and its message
docker pull <image> 2>&1 | tail -3                           # from a machine with the same credentials
```

Map the message to a layer of the protocol:

| Message contains | Layer | Check |
|---|---|---|
| `401 Unauthorized`, `unauthorized: authentication required` | token flow | `imagePullSecrets` on the pod/serviceaccount; `docker login`; does the token scope include this repository? |
| `403 Forbidden`, `denied: requested access to the resource is denied` | authorization | the credential exists but lacks pull on this repository (org/private repo, expired PAT) |
| `404`, `manifest unknown`, `name unknown` | tag/repository | `crane ls <repo>`; the tag was never pushed, pushed to another repo, or deleted |
| `no match for platform in manifest` / `exec format error` at run time | platform selection | `crane manifest <image>` shows the index; is `linux/amd64` (or your node's arch) present? Build with `buildx --platform` |
| `429 Too Many Requests`, `toomanyrequests` | rate limit | anonymous pulls are rate-limited; authenticate, use a pull-through cache or mirror, pre-pull |
| `digest mismatch`, `unexpected EOF`, `invalid checksum` | transport/corruption | a proxy or mirror is altering or truncating blobs; bypass it and pull by digest |
| `x509: certificate signed by unknown authority` | TLS | private registry CA not trusted by the node/containerd; add it, do not disable verification |

## 2. Replay the protocol by hand

```bash
crane manifest <image> | jq '.mediaType, (.manifests // [])[].platform'   # index vs manifest, platforms
crane digest <image>                                                       # what the tag points at right now
skopeo inspect --raw docker://<image> | jq .                                # same, different tool
skopeo inspect docker://<image> | jq '.Architecture, .Os, .Layers'
```

Without those tools, the three HTTP calls are: `GET /v2/<repo>/manifests/<tag>` (expect 401 with a Bearer challenge), `GET <realm>?service=…&scope=repository:<repo>:pull` (token), then the same manifest GET with `Authorization: Bearer <token>` and an `Accept` header listing the OCI index and manifest media types.

## 3. Fix at the right layer

- Auth: create or rotate the pull secret, attach it to the service account, verify with `kubectl get sa default -o yaml`.
- Tag drift: pin by digest (`image@sha256:…`) in manifests and let your CI update the digest; `:latest` is not a version.
- Platform: build multi-platform images in CI; add `nodeSelector` only as a stopgap.
- Rate limits: authenticate the nodes (registry credentials in the kubelet or a cluster-wide pull secret), run a pull-through cache, or mirror the base images you depend on.

## 4. Report

One line per layer checked (auth, manifest, platform, blobs), the command that proved it, and the fix applied. Include the digest you ended up running, so the next person can compare.
