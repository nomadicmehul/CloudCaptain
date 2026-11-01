---
name: policy-gate-design
description: Design, test and roll out a policy gate that evaluates OpenTofu or Terraform plan JSON between plan and apply, with deny and warn levels, exceptions, and a second engine kept in agreement
version: 1.0.0
phase: iac
lesson: iac/policy-as-code
tags: [opentofu, terraform, policy-as-code, opa, rego, conftest, ci, governance]
---

# Policy gate design

Use this when a team asks to "enforce our standards in CI", when a review missed a destroy, a missing tag or an unapproved module, or when an existing gate is so noisy that people bypass it. The gate judges the saved plan, never the diff alone.

## 1. Inventory what already exists

```bash
grep -rn --include='*.md' -i -E 'must|never|required|forbidden' docs/ wiki/ 2>/dev/null | head -50
ls policy/ .conftest* .checkov* .trivyignore 2>/dev/null
```

List every standard that exists on paper, in a scanner config or in reviewers' heads. For each, write one line: what it protects (money, data, access, pager), who owns it, and one real incident or near miss it would have caught. A rule without an owner or an incident is a candidate for `warn` or for deletion.

## 2. Express each standard as select, check, report

| Standard | Select | Check | Level to start |
|---|---|---|---|
| every resource has an owner | types that support tags, actions create and update | `tags.owner` exists | warn for two weeks, then deny |
| no oversized instances or disks | compute and volume types | size `not_in` a list, `disk_gb` `lte` a limit | deny |
| no world-readable storage or files | buckets, ACLs, `local_file` | public flags `equals false`, mode `not_matches` world bits | deny |
| protected resources are never destroyed | named addresses | actions contain `delete` | deny |
| stateful resources are never replaced | databases, volumes, buckets, queues | actions contain `delete` and `create` | deny |
| modules come from approved places | `configuration` module calls | `source` matches an allow-list; registry modules have `version_constraint` | deny |
| new tags or conventions | create only, so existing resources are grandfathered | `exists` or `matches` | warn |

For each attribute rule decide what an unknown value (`after_unknown` is `true`) means. Default to violation. Allow it only when the computed value cannot affect the property (an id, a timestamp).

## 3. Put the gate on the saved plan

```bash
tofu plan -input=false -lock-timeout=5m -out=plan.out
tofu show -json plan.out > plan.json
python3 planguard.py plan.json --rules policy/rules.json --markdown >> "$GITHUB_STEP_SUMMARY"
conftest test --parser json --policy policy/rego --output github plan.json
tofu apply -input=false plan.out
```

The apply consumes the file that was judged. Never re-plan between the gate and the apply. `plan.json` contains sensitive values in plain text: keep it out of public artifacts and delete it after the job.

## 4. Test the policy like code

- Commit fixture plans: one that passes every rule, one that violates each rule exactly once. Generate them from real `tofu show -json` output and trim them.
- One unit test per rule for the edge cases: attribute missing, value `null`, value known after apply, resource being replaced, resource inside a module (`module.x["a"].type.name`), data sources (`mode: "data"`) ignored.
- If two engines are in play (a Python or Go evaluator and Rego), assert that both return the same set of (level, rule, address) on every fixture. Compare sets, never message strings.
- `opa check policy/rego` and `conftest verify --policy policy/rego` in the same CI job as the tests.

## 5. Roll out without a revolt

| Situation | Do |
|---|---|
| new rule, existing estate violates it widely | `warn` with a dated note in the message; turn into `deny` on that date |
| new rule, estate already compliant | `deny` from day one |
| rule fires on something legitimate | exception with owner, reason, expiry; never edit the rule for one address |
| rule fires and nobody can explain why | treat as a bug in the rule; fix it before anything else |
| team asks to disable the gate | find the one noisy rule and downgrade it to `warn`; the gate stays |

Exceptions live in a reviewed file (`exceptions.json` or conftest `exception` rules) with `rule`, `address`, `reason`, `owner` and `expires`. An expired exception must start failing again.

## 6. Know which engine does which job

- Plan gate (this skill): conftest or `opa eval` over plan JSON, or Sentinel and OPA policy sets in HCP Terraform and Terraform Enterprise.
- Static scanning of source: Checkov, Trivy (`trivy config`). Cheap, broad, blind to defaults and module internals unless pointed at plan JSON.
- Kubernetes objects: Gatekeeper, Kyverno or `ValidatingAdmissionPolicy` at admission, because objects also arrive through `kubectl` and controllers that never pass through a plan.

## 7. Report

Post: the rules with id, level, owner and rollout date; the pipeline order (plan, show, gate, apply of the same file); the fixture plans and the test count; the results of the first run against every root module (deny and warn counts per module); the open exceptions with their expiry; and the one-line instruction for authors on how to read a failure and where to ask. Do not call the gate live until it has run on every root module once in `warn` mode.
