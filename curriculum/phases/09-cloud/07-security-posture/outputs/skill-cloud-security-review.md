---
name: cloud-security-review
description: Review the security posture of one AWS account against the CIS AWS Foundations Benchmark with a read-only collection, separate gaps from findings, triage by exposure, apply fixes in a safe order, and prove each fix by collecting again
version: 1.0.0
phase: cloud
lesson: cloud/security-posture
tags: [aws, security, cis, posture, security-hub, prowler, checkov, review]
---

# Cloud security posture review

Use this for a periodic review of an AWS account, before answering a customer's security questionnaire, after an incident, when inheriting an account, or when a pipeline's IaC scan is green and you need to know whether the running account agrees.

## 1. Collect with a role that can only read

```bash
aws sts get-caller-identity                                       # confirm the account and that the role is the audit role
aws iam list-attached-role-policies --role-name <audit-role>      # SecurityAudit (reads configuration), not ReadOnlyAccess (also reads data)
aws ec2 describe-regions --query 'Regions[].RegionName' --output text
for r in <regions in use>; do python3 posture.py collect --region "$r" -o "bundle-$r.json"; done
```

Write down the account ID, the Regions collected, the collection time and the benchmark version you will report against (state it as "CIS AWS Foundations Benchmark v5.0.0"; numbers differ in v3.0.0). Keep the bundles: they are the evidence, and next quarter's review diffs against them.

## 2. Read the gaps before the findings

```bash
python3 posture.py scan bundle-<region>.json --json | python3 -c 'import json,sys; [print(g["call"], "->", g["reason"]) for g in json.load(sys.stdin)["gaps"]]'
```

| Gap reason | Meaning | Action |
|---|---|---|
| `AccessDenied`, `UnauthorizedOperation` | the role cannot read this; the check did not run | add the permission or record the check as not assessed |
| `not collected` | the call was never made (older collector, wrong Region) | collect again |
| no `<root_account>` row | the credential report lacks root (emulators do) | check root in the console or with `get-account-summary` |
| `Policy is not valid JSON` | stored policy unreadable | read it by hand with `aws s3api get-bucket-policy` |

A section with gaps is "not assessed", never "passed". Say so in the report.

## 3. Scan and group by exposure

```bash
python3 posture.py scan bundle-<region>.json --benchmark 5.0.0
python3 posture.py scan bundle-<region>.json --now +30d --min-severity medium     # what expires in the next month (keys, unused credentials)
python3 posture.py scan bundle-<region>.json --markdown > findings.md
```

Triage every finding into one bucket, using the evidence, not the title:

| Bucket | Typical findings | Decision |
|---|---|---|
| Public now | `s3-public-acl` or `s3-public-policy` at critical, `sg-admin-*` on an instance with a public IP and an internet gateway route | fix today; check access logs for use |
| One step from account compromise | `root-access-key`, `root-mfa-off`, `user-console-no-mfa` for a human with admin, `ec2-imdsv1` on an instance with a role | fix this week; rotate anything that may have leaked |
| Blind spots | `cloudtrail-no-multi-region`, `config-not-recording`, `vpc-flow-logs-off`, `cloudtrail-no-validation` | fix this week: an incident without logs cannot be investigated |
| Missing guardrail | `s3-bucket-bpa-off`, `s3-account-bpa-off`, `ebs-default-encryption-off`, `default-sg-open` | fix in the next change window; prevents the next mistake |
| Hygiene | `user-key-old`, `user-two-active-keys`, `password-policy-*`, `kms-rotation-off` | schedule with an owner |
| Neutralised (low, "neutralised" in evidence) | public grant behind `IgnorePublicAcls` or `RestrictPublicBuckets` | remove the grant; the guardrail is the only thing holding |

Raise a finding one bucket when it combines with another into a path (an IMDSv1 instance whose role can read a bucket; an open admin port on an instance with an old key in its user data). Lower it only with evidence (the volume is empty and scheduled for deletion), never because "it has always been like that".

## 4. Cross-check with a second scanner

```bash
prowler aws --compliance cis_5.0_aws --region <region>            # open source, every CIS v5.0.0 check
aws securityhub get-findings --filters '{"ComplianceStatus":[{"Value":"FAILED","Comparison":"EQUALS"}],"RecordState":[{"Value":"ACTIVE","Comparison":"EQUALS"}]}' --max-items 100
checkov -d <terraform dir> --framework terraform --compact        # what the code would deploy
```

Where two scanners disagree, read both checks' logic before choosing. Known differences: Checkov's `CKV_AWS_67` fails every single-Region trail while CIS 3.1 asks for one logging multi-Region trail per account; Checkov 3.3.29's network ACL checks skip rules whose port range starts at 0; moto's `get-bucket-policy-status` treats any condition as non-public; Security Hub `EC2.21` ignores default network ACLs.

## 5. Fix in a safe order

1. Stop the bleeding without breaking traffic: account-level Block Public Access (`s3control put-public-access-block`) only after listing the buckets that are meant to be public (static sites behind CloudFront should use origin access control, not a public bucket).
2. Root: remove access keys and register MFA from the root sign-in; then nobody uses root.
3. Logging: start the multi-Region trail, enable validation, turn on Config recording, add flow logs. Do this before the noisy changes so they are recorded.
4. Network: revoke `0.0.0.0/0` and `::/0` on 22 and 3389 after confirming access through SSM Session Manager or a VPN; insert network ACL denies below the allow instead of deleting it.
5. Identity: MFA for console users, rotate or deactivate old and unused keys (create the new key, deploy it, then deactivate the old one), then the password policy.
6. Encryption: enable EBS default encryption in each Region; plan snapshot-copy migrations for unencrypted volumes; enable KMS rotation.

Use the `commands` from `posture scan --json` as the starting point and read each one before running it: `update-account-password-policy` replaces the whole policy, `revoke-security-group-ingress` removes exactly the range named, and `update-access-key --status Inactive` breaks whatever still uses the key.

## 6. Prove it

```bash
python3 posture.py collect --region <region> -o bundle-<region>-after.json
python3 posture.py scan bundle-<region>-after.json --json > after.json
python3 -c 'import json; a={(f["check"],f["resource"]) for f in json.load(open("before.json"))["findings"]}; b={(f["check"],f["resource"]) for f in json.load(open("after.json"))["findings"]}; print("resolved", len(a-b)); print("new", sorted(b-a))'
```

Every fixed finding must be gone from the new scan, or present at low with "neutralised" in its evidence. A new finding after a fix means the fix moved the problem.

## 7. Report

State: the account and Regions, the collection time, the benchmark version, and the role used; the gaps (sections not assessed and why); findings per triage bucket with the evidence line, the CIS section and the Security Hub control for each; the attack paths that combine findings; what was fixed during the review, proved by the after-scan; what remains, each with an owner, a date and, for accepted risks, a written reason and an expiry. Never report "compliant" for a section that had gaps, and never quote a CIS number without its version.
