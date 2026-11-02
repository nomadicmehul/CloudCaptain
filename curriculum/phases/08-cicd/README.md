# CI/CD & GitOps

*A pipeline runner and a GitOps reconciler, then GitHub Actions, GitLab CI, Argo CD, Flux and supply-chain security.*

A pipeline is the most privileged program most teams run: it holds deploy credentials, executes code from pull requests and decides what reaches production. When it breaks, the question is always mechanical: which job ran in which order with which inputs, which token it held, which artifact it built and whether anyone can prove it, and which loop moved the cluster to the new commit. This phase builds each of those mechanisms as a small tool (a workflow runner, a workflow auditor, an OIDC issuer and verifier, an SBOM generator, a signer with a transparency log, a GitOps reconciler, a canary controller and a release tool) and then runs the same job with the real one, so a failed run, a rejected token or an OutOfSync application reads as something you have written yourself.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| [Pipeline runner](01-pipeline-runner/docs/en.md) | Build | `minici`: a GitHub Actions-style runner with `needs`, `if` expressions, matrices, job outputs, clean working directories, a `hashFiles()` cache, artifacts and secret masking | GitHub Actions, `act`, `actionlint`, GitLab CI `needs` and `rules` |
| [Workflow security](02-workflow-security/docs/en.md) | Build | `wfaudit`: a static analyser for unpinned actions, `pull_request_target` checkouts, script injection, broad `permissions`, leaked secrets and cache poisoning, with SARIF output | zizmor, actionlint, `permissions:`, SHA pinning with Renovate or Dependabot |
| [OIDC from CI](03-ci-oidc/docs/en.md) | Build | `minioidc`: an OIDC issuer with JWKS and RS256 tokens built on `pow()`, and the cloud-side verifier that checks issuer, audience, time and trust-policy conditions on `sub` | `id-token: write`, `configure-aws-credentials`, `assume-role-with-web-identity`, workload identity federation |
| [SBOM](04-sbom/docs/en.md) | Build | `minisbom`: dpkg, apk, Python and npm inventories, purls, CycloneDX and SPDX output, SBOM diffs and advisory matching with per-ecosystem version comparison | syft, grype or OSV-Scanner, `buildx --sbom`, VEX |
| [Signing and provenance](05-signing-provenance/docs/en.md) | Build | `minisign`: ECDSA P-256 from the curve parameters, DSSE envelopes with SLSA provenance, and a Merkle transparency log with inclusion and consistency proofs | cosign `sign-blob`, `verify-blob`, attestations, Rekor, `gh attestation verify` |
| [GitOps reconciler](06-gitops-reconciler/docs/en.md) | Build | `minigitops`: a Git-watching reconciler with overlays, normalised diffs, server-side apply, pruning, sync waves, health and self-heal | Argo CD `Application` with automated sync, Flux, ApplicationSets |
| [Progressive delivery](07-progressive-delivery/docs/en.md) | Build | `minirollout`: blue/green and canary steps, weighted traffic, metric analysis with thresholds, automatic abort and promotion | Argo Rollouts, `AnalysisTemplate`, Flagger, Gateway API weights |
| [Releases](08-release-versioning/docs/en.md) | Build | `minirelease`: Conventional Commits, the next SemVer version, a grouped changelog, annotated tags and a dependency-update checker | git-cliff, release-please, semantic-release, Renovate and Dependabot |

Further lessons for this phase are proposed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md).

## Reference layer

The long-form guides, cheat sheets and interview questions for this phase live under [CI/CD](https://cloudcaptain.io/docs/tools/cicd), [GitOps](https://cloudcaptain.io/docs/tools/cicd/gitops), [GitHub Actions](https://cloudcaptain.io/docs/tools/github-actions) and [DevSecOps](https://cloudcaptain.io/docs/tools/devsecops). Jenkins is covered in the reference library as the enterprise incumbent; these lessons lead with GitHub Actions and GitLab CI.
