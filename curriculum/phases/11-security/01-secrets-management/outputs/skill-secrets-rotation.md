---
name: secrets-rotation
description: Rotate a secret that leaked or reached its age limit without an outage, by classifying it, containing it through lease and token revocation, rotating in a safe order, confirming through the audit log by hash, and replacing static strings with dynamic, expiring credentials
version: 1.0.0
phase: security
lesson: security/secrets-management
tags: [secrets, rotation, openbao, vault, leases, tokens, database, kubernetes, incident-response, audit]
---

# Secrets rotation

Use this when a secret has leaked (a log, a repository, a laptop backup, a screenshot, a scanner finding), when a person with access leaves, when a secret reaches its rotation age, or when someone asks "can we rotate X without downtime?". Do not paste secret values into chat, tickets or this runbook's report: refer to them by path, lease ID, accessor or audit hash.

## 1. Classify what leaked

```bash
export BAO_ADDR=https://openbao.example:8200          # and BAO_TOKEN with the needed policy
bao kv metadata get secret/PATH                        # a KV secret: versions, who wrote them when
bao lease lookup LEASE_ID                              # a dynamic credential: TTL, renewable
bao token lookup -accessor ACCESSOR                    # a token: policies, ttl, explicit_max_ttl, orphan
bao read sys/seal-status                               # unseal shares: type, t, n
```

| What leaked | Lives where | Contain with | Rotate with |
|---|---|---|---|
| dynamic credential (`database/creds/...`, cloud IAM keys from a secrets engine) | a lease | `bao lease revoke LEASE_ID`, or `bao lease revoke -prefix database/creds/ROLE` for everything a role issued | nothing: the next read issues a new one |
| token | the token store | `bao token revoke -accessor ACCESSOR` (children and their leases go too) | the client logs in again through its auth method |
| static secret in KV v2 | `secret/data/PATH` | restrict the policy or the token that can read it | write a new version, roll consumers, then `bao kv destroy` the old versions |
| database admin password used by the engine | the engine's connection config | none needed if only OpenBao knew it | `bao write -f database/rotate-root/NAME` |
| unseal key share or root token | operators | `bao token revoke` for a root token | `bao operator rekey` for shares; generate root tokens only for a task |
| string outside the secrets service (env var, CI variable, Kubernetes Secret, Git) | many copies | find every copy first (step 2) | step 3, then step 5 so it does not recur |

## 2. Find every copy and every use

```bash
git log -p --all -S 'FIRST-8-CHARS' --oneline | head                      # history, not just HEAD
kubectl get secrets -A -o json | jq -r '.items[] | select(.data != null) | .metadata.namespace + "/" + .metadata.name' > secret-list.txt
bao write -format=json sys/audit-hash/file input=@value.txt | jq -r .data.hash     # value.txt holds the value; delete it after
grep -c 'hmac-sha256:THATHASH' /var/log/openbao/audit.log                 # which requests carried it
```

The audit log never contains the value, only its HMAC under the device's key. Search for the hash to find which tokens read or wrote it and when; the `auth.accessor` field (also hashed) identifies the token. A value that has no hits was never read through the secrets service, which means it was used from a copy.

## 3. Rotate in an order that keeps the service up

1. Create the new credential first: a new KV version (`bao kv put -cas=N secret/PATH ...`, with `N` the version you read, so you do not overwrite a concurrent change), a new database password, a new API key at the provider. For provider keys, check the provider allows two active keys.
2. Roll the consumers: restart or re-sync the pods (External Secrets refreshes on `refreshInterval`; force with an annotation change), update CI variables, redeploy.
3. Verify every consumer uses the new credential (application health, database `pg_stat_activity` by user, provider "last used" timestamps).
4. Revoke the old one: at the provider, with `ALTER ROLE ... PASSWORD` in the database, or `bao kv destroy -versions=OLD secret/PATH`. Soft delete is not revocation: `bao kv undelete` brings it back.
5. Rewrite Git history only after the credential is dead, and treat the old value as public forever.

| Situation | Decision |
|---|---|
| leak is public (repository, bucket, paste site) | revoke first, accept the outage, then rotate; attackers scrape public leaks within minutes |
| leak is internal (log with limited access) and the secret supports two active versions | steps 1 to 4 in order today |
| consumers cannot reload without a deploy | revoke at the end of a fast deploy window today; do not wait for the next release |
| the same string is shared by several services | give each service its own credential during the rotation; never rotate to another shared string |
| the secret is a dynamic credential | revoke the lease (or the prefix); nothing else to do |
| unseal shares or a root token were exposed | rekey or revoke now, then review the audit log for root-token use since the exposure |

## 4. Confirm

```bash
bao lease lookup LEASE_ID                 # 400 invalid lease after revocation
bao kv metadata get secret/PATH           # old versions destroyed: true
grep -c 'hmac-sha256:OLDHASH' /var/log/openbao/audit.log    # stops growing after the revocation time
```

Try the old credential once from a safe place and expect authentication to fail. For databases, connect over the network, not over a local socket that the server trusts without a password.

## 5. Make the next rotation an expiry

| Today | Replace with |
|---|---|
| database password in an env var | database secrets engine role with `default_ttl` of hours, the app reads `database/creds/ROLE` at start and renews its lease |
| cloud access key in a CI variable | the CI platform's OIDC token exchanged for short-lived cloud credentials |
| token with no `explicit_max_ttl` | auth method (Kubernetes, OIDC, AppRole) issuing tokens with `explicit_max_ttl` |
| Kubernetes Secret written by hand | External Secrets Operator from OpenBao, plus encryption at rest with a KMS provider |
| static API key that the provider cannot make dynamic | KV v2 with `max_versions`, a rotation date in `custom_metadata`, and an alert on its age |

## Report

```
Secret: <path, lease ID prefix, accessor or description; never the value>
Class: <dynamic | token | KV static | engine root | unseal/root | outside the service>
Exposure: <where, since when, public or internal>
Contained: <time, command used (revoke lease/token, policy change)>
Copies found: <count and locations>
Uses since exposure: <audit hash hits by accessor, unknown sources>
Rotated: <new credential created, consumers rolled, old revoked: times>
Verified: <old credential refused at time, new in use by N consumers>
Follow-up: <the replacement from step 5, owner, date>
```
