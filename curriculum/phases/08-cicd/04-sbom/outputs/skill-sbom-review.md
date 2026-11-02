---
name: sbom-review
description: Review what an image change really ships by diffing the SBOMs of the old and new digest, and triage a new advisory across every running digest with source-package and version-order checks, ending in a fix ticket or a VEX statement per finding
version: 1.0.0
phase: cicd
lesson: cicd/sbom
tags: [sbom, cyclonedx, spdx, syft, grype, osv, vex, supply-chain, containers, ci]
---

# SBOM review

Use this in two situations. **A change review**: a pull request or release changes an image (a `FROM` line, a package install, a lock file, a build flag) and someone has to say what the new digest ships that the old one did not. **An advisory sweep**: a new advisory or CVE lands and someone asks which running images are affected. Work from digests, never from tags: a tag can point at a different image tomorrow, a digest cannot.

## 1. Get one SBOM per digest

Prefer the SBOM the build attached; generate one only when it is missing.

```bash
OLD=registry.example.com/team/app@sha256:...    # what runs now
NEW=registry.example.com/team/app@sha256:...    # what the change builds
docker buildx imagetools inspect "$OLD" --format '{{ json .SBOM.SPDX }}' > old.spdx.json || true
docker buildx imagetools inspect "$NEW" --format '{{ json .SBOM.SPDX }}' > new.spdx.json || true
syft scan registry:"$OLD" -o cyclonedx-json=old.cdx.json      # when no attestation exists
syft scan registry:"$NEW" -o cyclonedx-json=new.cdx.json
docker create --name probe "$NEW" && docker export probe | python3 minisbom.py scan - --json > new.minisbom.json; docker rm probe
```

| What you see | Meaning | Next |
|---|---|---|
| attestation present, `predicateType` SPDX or CycloneDX | the build recorded its own inventory | use it; note the generator and its version from `creationInfo` or `metadata.tools` |
| no attestation | nobody recorded what was built | generate with syft, and open a ticket to add `--sbom=true` (or a syft step) to the build |
| the SBOM is much smaller than the image suggests | package metadata was deleted or never written (`rm -rf /var/lib/apk`, binaries copied from another stage, `curl` and `tar` installs) | treat as a finding; compare with `syft scan --scope all-layers` and list the binaries syft classifies |
| two tools disagree on a package set | different catalogers (image versus directory scan, dev dependencies, binary classifiers) | compare by (type, name, version) parsed from both; never compare purl strings or JSON text |

## 2. Change review: diff and classify

```bash
python3 minisbom.py diff old.cdx.json new.cdx.json --json > diff.json; echo "exit $?"   # 1 means something changed
python3 minisbom.py diff old.cdx.json new.cdx.json                                         # + added, - removed, ~ changed (upgrade, downgrade)
```

Classify every line before approving:

| Change | Typical cause | Verdict |
|---|---|---|
| many `deb` or `apk` changes, no package line in the diff of the pull request | base image moved (new tag, rebuilt tag, new distro release) | expected only if the pull request says so; check the `os` line and the distro release in the purls |
| a change the pull request names | the intended upgrade | confirm the version is the one requested and that nothing else in its ecosystem moved |
| `downgrade` | a cached layer, an older base digest, a resolver picking a lower version | block until explained |
| a removed package that something still imports | a slimmer base or a purge | run the service's smoke test against the new digest before merging |
| a whole ecosystem missing (`npm 0` where it was 40) | a lock file not in the build context (`.gitignore`, `.dockerignore`) or metadata deleted | block: the next advisory sweep will be blind for this image |
| `RECORD` mismatch from `scan --verify-records` | a file edited after `pip install` | block and find out who patched it in the image |

## 3. Match both sides

```bash
python3 minisbom.py match old.cdx.json --advisories osv/ --json > old-findings.json
python3 minisbom.py match new.cdx.json --advisories osv/ --json > new-findings.json
grype sbom:./new.cdx.json --only-fixed -o table
```

Report three sets: findings the change **fixed** (in old, not in new), findings it **introduced** (in new, not in old), and findings that **remain**. A change that fixes one critical and introduces two highs is not an improvement by default.

## 4. Advisory sweep: who is affected

```bash
kubectl get pods -A -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{.imageID}{"\n"}{end}{end}' | sort -u > digests.txt
while read -r digest; do
  docker buildx imagetools inspect "$digest" --format '{{ json .SBOM.SPDX }}' > "sbom-${digest##*:}.json" 2>/dev/null || syft scan registry:"$digest" -o cyclonedx-json="sbom-${digest##*:}.json"
done < digests.txt
for f in sbom-*.json; do python3 minisbom.py match "$f" --advisories advisory.json --json > "hit-${f#sbom-}"; done
```

For every hit, check the three things a matcher can get wrong, by hand:

| Check | How | Wrong when |
|---|---|---|
| ecosystem and release | the advisory's `ecosystem` (`Debian:12`, `Alpine:v3.22`, `PyPI`, `npm`) against the purl's type and `distro` qualifier | a `Debian:13` range applied to a bookworm image, or an upstream CVE applied to a distro package the distro already patched |
| name | binary name, or source name for `deb` and `apk` (`upstream` qualifier, dpkg `Source`, apk `o:`) | the advisory names `openssl` and the SBOM has `libssl3`: that is a real match, not a false positive |
| version order | `dpkg --compare-versions A lt B`, `apk version -t A B`, `python3 -c 'from packaging.version import Version as V; print(V("A") < V("B"))'`, npm `semver` | anyone compared strings: `3.0.11-1~deb12u2` is older than `3.0.11-1`, `1.0.0-beta.11` is newer than `1.0.0-beta.2` |

## 5. Decide per finding

| Situation | Action |
|---|---|
| a fixed version exists in the same ecosystem and release | upgrade (rebuild on a newer base digest or bump the package), then rerun steps 1 to 3 |
| no fixed version yet | mitigate (configuration, network policy, feature off) and record `under_investigation` with a review date |
| the vulnerable code is not reachable in this product | write a VEX statement: status `not_affected`, a justification (`component_not_present`, `vulnerable_code_not_present`, `vulnerable_code_not_in_execute_path`, `vulnerable_code_cannot_be_controlled_by_adversary`, `inline_mitigations_already_exist`), the product purl with the digest, the package purl, an author and a timestamp; schedule the upgrade anyway |
| the match is wrong (wrong release, wrong name, wrong order) | record it as a scanner bug with the evidence from step 4; do not suppress it globally |

```bash
grype sbom:./new.cdx.json --vex ./vex.json     # confirm the statement suppresses exactly the intended finding
```

## 6. Report

State: the old and new digests; where each SBOM came from (attestation or generated, tool and version); the diff summary (counts per ecosystem, the `os` line, every downgrade and every missing ecosystem); findings fixed, introduced and remaining, each with advisory id, package, matched name (binary or source), installed and fixed versions and the version order used; the decision per finding (upgrade, mitigate, VEX with justification and review date, scanner bug); and for an advisory sweep, the list of affected digests with the workloads that run them. Never report "scanned the latest tag" as evidence for what runs in production.
