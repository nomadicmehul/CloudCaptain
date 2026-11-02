---
name: vuln-triage
description: Turn container image scan results into a short, defensible queue by merging Trivy and Grype findings, adding KEV and EPSS from dated feeds, asking for the workload's exposure, deciding reachability with evidence, assigning priorities and due dates, choosing the cheapest fix path and proving it with a diff
version: 1.0.0
phase: security
lesson: security/vuln-prioritization
tags: [security, vulnerabilities, cvss, epss, kev, vex, openvex, trivy, grype, sla, containers]
---

# Vulnerability triage

Use this when a scan of an image fails a pipeline, a dashboard shows hundreds of open findings, a new advisory lands (a KEV addition, a vendor bulletin), or someone asks "do we have to fix this, and by when?". The goal is a queue in which every row has a reason, an owner and a date, and every suppression has evidence. Never lower a priority or suppress a finding without writing down why.

## 1. Scan, and record what you scanned with

```bash
IMAGE=<registry/repo@sha256:digest>        # a digest, not a tag: tags move
trivy image --scanners vuln --format json --output trivy.json "$IMAGE"
grype "$IMAGE" -o json --file grype.json
trivy version --format json | jq '.VulnerabilityDB'       # DB version and UpdatedAt
grype db status                                            # DB build date and schema
```

Write down the image digest, both tool versions and both database dates. A finding that appears or disappears between two days with no change to the image came from the database, not from you.

## 2. Merge and count once

```bash
python3 minivuln.py ingest trivy.json grype.json | head -3
python3 minivuln.py ingest trivy.json grype.json --json > merged.json
```

| What you see | Meaning | Next |
|---|---|---|
| a finding in both tools | strongest inventory evidence | continue |
| only Trivy or only Grype | different database or matcher | check the package exists (`docker run --rm IMAGE dpkg -s PKG`, `pip show PKG`); do not drop it, mark the source |
| Grype `matchDetails[].type` is `cpe-match` | matched by a guessed product name | verify the package really is that product before acting |
| same flaw under CVE and GHSA ids | one finding, two names | report under the CVE; KEV and EPSS are keyed by CVE |
| `MISMATCH reported X, computed Y` | a source's score does not fit its vector | trust the vector; tell the source if it is your vendor |

## 3. Add the facts the scanner cannot see

```bash
curl -fsSL -o kev.json https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json
curl -fsSL -o epss.csv.gz https://epss.empiricalsecurity.com/epss_scores-current.csv.gz
jq -r .catalogVersion kev.json; gzip -dc epss.csv.gz | head -1
```

Ask the service owner for the exposure of every deployment of this image: `internet` (reachable from outside, directly or through a load balancer), `internal` (only from your networks), `isolated` (no inbound path, batch or air-gapped). If one image has several deployments, triage for the most exposed one. Record the KEV catalogue version and the EPSS score date in the report.

## 4. Decide reachability, with evidence

For each finding that would be P1 or P2, and for any finding someone wants to suppress:

| Question | Evidence that answers it | Outcome |
|---|---|---|
| Is the package really present in the running image? | `docker run --rm IMAGE dpkg -s PKG` / `apk info -e PKG` / the file path in `locations` | absent: `component_not_present` |
| Is the vulnerable code compiled in? | the advisory names a file, module or build option; the package's build flags | not built: `vulnerable_code_not_present` |
| Does our code path call it? | grep for the function, imports, call graph, a failing exploit attempt in staging | never called: `vulnerable_code_not_in_execute_path` |
| Can an attacker control its input? | where the input comes from (config, a fixed allow-list, user requests) | not controllable: `vulnerable_code_cannot_be_controlled_by_adversary` |
| Does something block it for good? | a mitigation that cannot be configured away (not a WAF rule) | `inline_mitigations_already_exist` |
| Not sure yet | | `under_investigation` with an owner and a date |

Write the decision as OpenVEX so every scanner honours it:

```bash
vexctl create --author security@example.com --product "pkg:TYPE/NAMESPACE/NAME@VERSION" \
  --vuln CVE-YYYY-NNNN --status not_affected --justification vulnerable_code_not_in_execute_path \
  --impact-statement "<what you checked and where>" --file vex/CVE-YYYY-NNNN.json
python3 minivuln.py vex vex/*.json            # exit 1 lists every invalid statement
```

Use the package purl without qualifiers as the product, or the image as the product with the package as a subcomponent. A statement whose product is the image and that has no subcomponents suppresses that CVE in every package of the image. A KEV entry needs a second reviewer before any `not_affected`.

## 5. Rank and date

```bash
python3 minivuln.py rank trivy.json grype.json --kev kev.json --epss epss.csv.gz \
  --vex vex/*.json --exposure internet --top 20
python3 minivuln.py rank trivy.json grype.json --kev kev.json --epss epss.csv.gz --exposure internet --fail-on P1   # the CI gate
```

| Priority (default policy) | Rule | Due |
|---|---|---|
| P1 | in KEV, whatever the exposure | 7 days from first seen |
| P2 | EPSS x exposure weight (internet 1.0, internal 0.5, isolated 0.1) at least 0.1 | 30 days |
| P3 | CVSS 7.0 or more | 90 days |
| P4 | everything else | 180 days, or the next base image refresh |
| suppressed | VEX `not_affected` or `fixed` | review date on the statement |

If your organisation has a different policy, encode it as a `--policy` file instead of overriding rows by hand.

## 6. Choose the cheapest fix

| Situation | Fix | Check |
|---|---|---|
| many OS findings, newer base image fixes them | bump the base image digest and rebuild | `minivuln diff` shows them fixed |
| one OS package behind | `apt-get install --only-upgrade PKG` in the Dockerfile, then remove it at the next base bump | the package version in the new SBOM |
| language dependency | bump it in the lock file to the smallest fixed version | the lock file diff and the tests |
| no fixed version (`wont-fix`, `not-fixed`) | remove the package if unused, switch base (distroless, slim), or mitigate and accept with an expiry | the package is gone, or an exception with an owner |
| fix exists but breaks the app | pin a mitigation, open an upstream issue, set the exception expiry inside the SLA | the date is in the tracker |

## 7. Prove it

```bash
trivy image --scanners vuln --format json --output trivy-new.json "$NEW_IMAGE"
grype "$NEW_IMAGE" -o json --file grype-new.json
python3 minivuln.py diff --old trivy.json grype.json --new trivy-new.json grype-new.json \
  --kev kev.json --epss epss.csv.gz --exposure internet
```

Use the same tools on both sides (minivuln warns otherwise). Every P1 and P2 you acted on must be under `fixed`; anything under `new` gets triaged now; `still open` keeps its first-seen date.

## 8. Report

State: the image digest, tool versions, database dates, KEV catalogue version and EPSS score date; the exposure and who confirmed it; the count before and after merging and the overlap; a table of P1 and P2 findings with id, package, installed and fixed version, the deciding signal (KEV, EPSS x exposure, CVSS), due date and overdue days; every VEX statement added, with its justification, evidence and reviewer; the fix path chosen and the diff that proves it; and the exceptions still open with owners and expiry dates. Never report "fixed" for a finding that only disappeared because a tool or database changed.
