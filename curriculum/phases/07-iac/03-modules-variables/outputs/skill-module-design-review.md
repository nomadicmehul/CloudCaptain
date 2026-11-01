---
name: module-design-review
description: Review a new or changed OpenTofu or Terraform module for a safe interface, stable instance keys, pinned sources and refactors that move state instead of destroying it
version: 1.0.0
phase: iac
lesson: iac/modules-variables
tags: [opentofu, terraform, iac, modules, variables, refactoring, review]
---

# Module design review

Use this when a pull request adds a module, changes a module's variables or outputs, renames or moves resources or module calls, or changes `count` or `for_each`. A module is a function: review its interface like an API and its addresses like database keys. Work from a real plan for every environment that calls the module, never from the diff alone.

## 1. Collect the facts

```bash
tofu init -input=false                       # or terraform; note every module source and version it resolves
cat .terraform/modules/modules.json          # which directory each module key loaded from
tofu validate
tofu state list > before.txt                 # per environment, before the change
tofu plan -input=false -var-file=ENV.tfvars -out=plan.out
tofu show -json plan.out > plan.json
```

If you cannot run plans (no credentials), say so, review the configuration only, and mark every refactor finding as unverified.

## 2. Inputs: every variable is a parameter

For each `variable` the change adds or touches:

- **Type**: as narrow as the data. `string`, `number`, `bool`, `list(string)`, `map(object({...}))` with named attributes; `any` only when the module genuinely passes values through. A loose type moves the error from plan to the provider API.
- **Validation**: enumerations (`contains(["dev", "prod"], var.env)`), formats (`can(regex(...))`), ranges and lengths. The `error_message` states the rule and an example of a valid value. Missing validation on a value that reaches a cloud API is a finding.
- **Default**: safe for every caller or absent. No production sizes, regions or account ids as defaults; a value that must differ per environment is required.
- **Sensitive**: secrets, tokens and connection strings are `sensitive = true`. Check that no secret has a default in code.
- **Description** on every variable; the module's README or docs list required inputs first.

## 3. Outputs: return only what callers need

- Expose ids, names and endpoints, not whole resource objects; a whole object couples callers to every attribute.
- An output derived from a sensitive value is `sensitive = true`. Remember that state holds it in clear either way.
- Renaming or removing an output breaks every caller. Treat it as a breaking change and bump the module's major version.

## 4. Instance keys: count or for_each

| Situation | Use | Why |
|---|---|---|
| N identical copies, the number is all that matters | `count` | positions are fine when items are interchangeable |
| A collection of distinct things (sites, users, regions, buckets) | `for_each` over a map or `toset()` | removing one item removes one instance instead of shifting the rest |
| Optional single resource | `count = var.enabled ? 1 : 0` | the address becomes `x[0]`; add a `moved` block if it existed without `count` |
| Keys computed from another resource's attributes | not allowed | unknown during plan; key by configuration values instead |
| Keys derived from a sensitive value | not allowed | keys appear in every address |

Flag any `count = length(var.list)` over distinct items and ask for `for_each` with a `moved` block per existing instance.

## 5. Sources and call sites

- Child modules contain no `provider` blocks; providers are configured in the root and passed with `providers = {...}` when aliases are needed.
- Registry sources have a `version` constraint (`~> 5.1` allows 5.x from 5.1); modules you do not own are pinned exactly. Git sources pin `?ref=` to a tag or commit, never a branch. `.terraform.lock.hcl` locks providers only, not modules.
- Local sources start with `./` or `../`; anything else is resolved from a registry.

## 6. Refactors move, they do not recreate

Any destroy paired with a create of the same type is a rename until proven otherwise:

```bash
python3 - <<'PY'
import json
plan = json.load(open("plan.json"))
for change in plan["resource_changes"]:
    actions = change["change"]["actions"]
    if actions != ["no-op"] or change.get("previous_address"):
        print(actions, change.get("previous_address", ""), "->", change["address"])
PY
```

Require a `moved` block for each pair (or one block per renamed module call), then re-plan every environment and confirm the plan shows the moves and `0 to destroy` for those resources. Moving into a module, out of a module, and from no key to a `for_each` key are all `moved` blocks; changing the resource type is not, and needs a `removed` block that forgets the old address without destroying it, plus an `import` block for the new type. Reject `tofu state mv` as the plan for shared code: it is not reviewed and must be repeated in every state.

## 7. Decide

| Finding | Verdict |
|---|---|
| Stateful resource (database, volume, bucket, queue, key, DNS zone) destroyed by a rename | **DO NOT MERGE** until a `moved` block makes it a move |
| `count` over distinct items in a new module | **CHANGES REQUESTED** |
| Variable reaching an API without validation or with an unsafe default | **CHANGES REQUESTED** |
| Secret without `sensitive = true`, or with a default in code | **CHANGES REQUESTED** |
| Provider block inside a child module, unpinned registry or git source | **CHANGES REQUESTED** |
| Output renamed or removed without a major version bump | **CHANGES REQUESTED** |
| Everything above clear and every environment's plan matches the stated intent | **APPROVE** |

## 8. Report

Post one review comment with: the verdict; a table of inputs (name, type, default, validation, sensitive) for changed variables with the problem in each row that has one; the instance-key decision and any shifts it causes; every destroy-and-create pair with the `moved` block that fixes it; the plan summary line per environment before and after the fix; and the commands you ran. If a plan was not run, say which environments are unverified.
