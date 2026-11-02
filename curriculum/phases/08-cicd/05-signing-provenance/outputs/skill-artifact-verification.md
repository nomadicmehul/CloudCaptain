---
name: artifact-verification
description: Decide whether an artifact, image or package may be deployed, installed or mirrored by verifying its signature or keyless identity against trust roots fetched out of band, its SLSA provenance against a policy that pins subject, builder and source, and its transparency log entry; includes the key-compromise steps and a fixed report
version: 1.0.0
phase: cicd
lesson: cicd/signing-provenance
tags: [supply-chain, sigstore, cosign, slsa, provenance, in-toto, dsse, rekor, transparency-log, attestations, verification]
---

# Artifact verification

Use this before an artifact crosses a trust boundary: a deploy, a mirror into an internal registry, a vendor download, a release promotion, or when someone asks "is this build ours?". Verify the bytes you will actually use: download first, then verify the downloaded file or the image digest, never a tag or a URL you will fetch again later.

## 1. Pin the artifact and see what came with it

```bash
ART=dist/app.tar.gz
sha256sum "$ART"                                                     # the identity of everything below
python3 minisign.py inspect --json app.sigstore.json                 # or: jq '.mediaType, (.verificationMaterial | keys)' app.sigstore.json
crane digest registry.example/acme/app:1.4.2                         # images: resolve the tag once, then use NAME@sha256:...
```

| What you have | Meaning | Verify with |
|---|---|---|
| `messageSignature` and `verificationMaterial.publicKey` | key-based signature over the bytes | step 3a, a key you obtained separately |
| `messageSignature` and a `certificate` or `x509CertificateChain` | keyless signature (Fulcio certificate) | step 3b, an identity and an issuer |
| `dsseEnvelope` with payloadType `application/vnd.in-toto+json` | an attestation (provenance, SBOM, VEX) | step 3 for the signer, then step 4 for the content |
| `tlogEntries` with `inclusionProof` and `checkpoint` | published to a transparency log, provable offline | step 5 |
| `tlogEntries` with only `inclusionPromise` (bundle v0.1) | the log promised inclusion; no proof in hand | step 5 online, or fetch the proof from the log |
| a bare `.sig` file and a `.pub` next to the artifact | a detached signature; the key from the same place proves nothing | step 2 before anything else |
| nothing | unsigned | stop: report it as unverified, do not "verify" by checksum from the same server |

## 2. Get the trust roots from somewhere else

Never take the key, the expected identity or the checksum from the same location as the artifact; whoever can replace the artifact can replace those too.

| Trust root | Where it should come from |
|---|---|
| public key | a pinned fingerprint in your repository or policy (`minisign.py public-key --key K --json` prints `sha256:...` of the SPKI), a KMS, or the vendor's documentation served from a different origin |
| keyless identity | the exact workflow path and ref (`https://github.com/OWNER/REPO/.github/workflows/release.yml@refs/heads/main`) and issuer (`https://token.actions.githubusercontent.com`) written in your policy |
| Sigstore trusted root | TUF (cosign does this by default) or a reviewed `trusted_root.json` for air-gapped verification with `--trusted-root` |
| expected builder and source | your policy file: `builderId`, `source`, `buildType` |

## 3. Verify who signed

**3a. With a key**

```bash
cosign verify-blob --key release.pub --bundle app.sigstore.json "$ART"                          # add --insecure-ignore-tlog only for private, unlogged signing
python3 minisign.py verify-blob --key release.pub --bundle app.sigstore.json "$ART"             # an independent second verifier
cosign verify --key release.pub registry.example/acme/app@sha256:DIGEST                         # images
```

**3b. Keyless**

```bash
cosign verify-blob --bundle app.sigstore.json \
  --certificate-identity https://github.com/acme/app/.github/workflows/release.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com "$ART"
gh attestation verify "$ART" --repo acme/app --signer-workflow acme/app/.github/workflows/release.yml
```

Use `--certificate-identity-regexp` only with an anchored expression (`^https://github\.com/acme/app/\.github/workflows/release\.yml@refs/tags/v`); an unanchored regexp matches forks and other repositories.

## 4. Verify how it was built

```bash
cosign verify-blob-attestation --key release.pub --bundle app.att.sigstore.json --type slsaprovenance1 "$ART"
python3 minisign.py verify-attestation --key release.pub --bundle app.att.sigstore.json --artifact "$ART" --policy policy.json
gh attestation verify "$ART" --repo acme/app --signer-workflow acme/app/.github/workflows/release.yml --source-ref refs/heads/main --deny-self-hosted-runners
```

A policy that passes must have checked, in this order:

1. the envelope signature, before any field of the payload was read;
2. `payloadType` is `application/vnd.in-toto+json` and `predicateType` is the one you expect (`https://slsa.dev/provenance/v1`);
3. a `subject` digest equals the digest from step 1 (not the name: names are free text);
4. `runDetails.builder.id` (v0.2: `builder.id`) is the builder you pinned;
5. `resolvedDependencies` (v0.2: `materials`) contains your repository at the expected ref, with a `gitCommit`;
6. optionally `buildType` and `externalParameters` (the workflow path, no unexpected inputs).

`cosign verify-blob-attestation` alone covers 1 to 3. Items 4 and 5 need a policy: minisign's `--policy`, `gh attestation verify --signer-workflow`, Kyverno `verifyImages` attestation conditions, or policy-controller's `ClusterImagePolicy`.

## 5. Verify publication in the transparency log

```bash
python3 minisign.py verify-blob --key release.pub --bundle app.sigstore.json --log-key rekor.pub "$ART"
python3 minisign.py log verify --log-key rekor.pub --entry tlog-entry.json --bundle app.sigstore.json
curl -s https://rekor.sigstore.dev/api/v1/log                     # the public-good log's current tree size and signed checkpoint
```

Every entry must pass: log ID equals the trusted log key, inclusion proof leads to the root, the checkpoint is signed and states the same size and root, and the logged entry holds this bundle's digest and signature. If you run a monitor, also check consistency from the last checkpoint you trusted (`log verify-consistency --trusted-checkpoint`).

## 6. Decide

| Result | Decision |
|---|---|
| signature, provenance policy and log entry all pass | allow; record the digest, signer and builder in the report |
| signature passes, no provenance | allow only under an explicit, dated exception; this is the stolen-key case the provenance exists to catch |
| provenance subject does not match the artifact digest | block: the attestation describes another artifact |
| builder or source does not match the policy | block and escalate: built somewhere else, from something else |
| keyless identity matches but issuer differs | block: same name, different identity provider |
| verified only with `--insecure-ignore-tlog` or `--insecure-ignore-chain` | allow only for private infrastructure that you documented; never for public artifacts |
| consistency proof from a remembered checkpoint fails | incident: the log rewrote or forked history; stop trusting it and compare checkpoints with other monitors |
| the only key or checksum came with the artifact | unverified; get the trust root from step 2 |

## 7. Suspected key compromise

1. Stop signing with the key and remove it from every secret store and runner.
2. Search the log for its use: on Rekor v1, `rekor-cli search --public-key release.pub --pki-format x509`, or watch your own log with a monitor; list every entry since the key was created.
3. Compare each entry's digest with your release records; any digest you did not release is an attacker's artifact: publish it as an indicator of compromise.
4. Move to keyless signing pinned to the release workflow, or to a KMS key with a separate audit log, and publish the new identity through a channel the attacker did not control.
5. Re-verify every artifact still in use against provenance from the release builder, not against the old key.

## 8. Report

State: the artifact name and its SHA-256 digest; the signature material found (bundle version, key or keyless, log entries); the trust roots used and where they came from; each check from steps 3 to 5 with pass or fail and the tool that ran it; the builder, source repository, commit and invocation from the provenance; any insecure flag and its justification; and the decision from step 6. Never report "signature valid" without naming the key fingerprint or the identity it was valid for.
