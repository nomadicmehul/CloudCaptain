---
name: review-terraform-plan
description: Review a Terraform or OpenTofu plan for destructive actions, data loss, ordering risks and drift before it is applied
phase: iac
lesson: iac/state-reconciler
---

You are reviewing an infrastructure change before it is applied. I will paste the output of `terraform plan` (or `tofu plan`, or `terraform show -json plan.out`). Produce exactly these sections and nothing else.

**Verdict**
One line: `SAFE TO APPLY`, `APPLY WITH CARE`, or `DO NOT APPLY`, followed by the single most important reason.

**Destructive actions**
Every resource that is destroyed (`-`) or replaced (`-/+`), as a table: address, resource type, the attribute that forces replacement (quote the `# forces replacement` line), and whether the type holds data (databases, volumes, buckets, queues, secrets, DNS zones). For each stateful resource say what is lost and whether a backup or `prevent_destroy` exists in the plan.

**Drift and surprises**
Changes that were not expected from the stated intent: resources touched outside the module being changed, `known after apply` values on security-relevant attributes (IAM policies, security groups, public access flags), provider or version changes, and anything in the plan that updates more resources than the summary line suggests.

**Ordering and blast radius**
Dependencies that make the apply risky: replacements of resources that others depend on, changes to shared networking, IAM, or DNS, and the count of resources affected per environment.

**Questions for the author**
At most five, each answerable with a sentence, each tied to a specific line of the plan.

Rules: quote plan lines verbatim when you cite them; never assume attributes that are not in the plan; if the plan is truncated say which sections you could not see. Keep it under 400 words.
