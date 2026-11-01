---
name: plan-review
description: Gate a Terraform or OpenTofu plan in CI or in a pull request, blocking on destroys of stateful resources, unexplained replacements and drift
version: 1.0.0
phase: iac
lesson: iac/state-reconciler
tags: [terraform, opentofu, iac, ci, review]
---

# Plan review

Use this when asked to review, approve or explain an infrastructure plan, or when a PR touches `*.tf` files. Work from a real plan, never from the diff alone.

## 1. Get a machine-readable plan

```bash
terraform init -input=false
terraform plan -input=false -lock-timeout=5m -out=plan.out
terraform show -json plan.out > plan.json
```

Use `tofu` in place of `terraform` on OpenTofu projects. If you cannot run the plan (no credentials), say so and review the pasted human-readable plan instead; do not guess.

## 2. Classify every resource change

From `plan.json`, read `resource_changes[].change.actions`:

| actions | meaning | default stance |
|---|---|---|
| `["create"]` | new resource | allow |
| `["update"]` | in-place change | allow; inspect security attributes |
| `["delete", "create"]` or `["create", "delete"]` | replacement | **inspect**: find `replace_paths` and the forcing attribute |
| `["delete"]` | destroy | **block** if the type is stateful (below) unless the PR says so explicitly |
| `["no-op"]` | unchanged | ignore |

Stateful types to treat as data-bearing: anything matching `*_db_*`, `*database*`, `*_rds_*`, `*_sql_*`, `*bucket*`, `*storage_account*`, `*_volume*`, `*_disk*`, `*queue*`, `*topic*`, `*secret*`, `*kms_key*`, `*dns_zone*`, `*_cluster` (Kubernetes, Kafka, Redis, Elasticsearch).

## 3. Look for the three classic traps

- **Rename that destroys**: a resource renamed in code appears as destroy + create. Ask for a `moved { from = … to = … }` block instead.
- **Known-after-apply on security attributes**: IAM policies, security group rules, `publicly_accessible`, `acl`, `block_public_acls` computed at apply time. Ask the author to make them literal or to show the resulting value.
- **Plan touches more than the PR says**: compare the plan summary with the PR description and the files changed; drift repairs and provider upgrades should be their own PRs.

## 4. Decide and write it down

Post a review comment with: the verdict (`SAFE TO APPLY` / `APPLY WITH CARE` / `DO NOT APPLY`), the plan summary line, the table of destroys and replacements with the forcing attribute, and up to five concrete questions. Approve only `SAFE TO APPLY`. Never approve a plan you did not read in full; if the plan is longer than you can read, say so and ask for it to be split.

## 5. After apply

Ask for `terraform apply plan.out` (the saved plan, not a fresh one) so what was reviewed is what runs. If the apply output differs from the plan, that is an incident, not a footnote.
