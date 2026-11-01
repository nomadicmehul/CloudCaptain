---
name: drift-triage
description: Triage infrastructure drift in an OpenTofu or Terraform stack by separating it from unapplied changes, classifying each finding by what the next apply would do, finding who changed it, and choosing revert, accept, ignore or adopt
version: 1.0.0
phase: iac
lesson: iac/drift-detection
tags: [opentofu, terraform, iac, drift, kubernetes, ci, github-actions, incident]
---

# Drift triage

Use this when a nightly drift job fails or opens an issue, when a plan for a small change shows updates or replacements nobody asked for, or when someone admits to "a quick fix in the console". Work from real plans, never from the configuration alone, and never paste raw plan JSON anywhere: `tofu show -json` prints sensitive values in plain text.

## 1. Get both plans for the stack

```bash
cd stacks/<stack>
tofu init -input=false
tofu plan -refresh-only -detailed-exitcode -input=false -lock-timeout=5m -out=drift.tfplan; echo "refresh-only exit $?"
tofu plan -detailed-exitcode -input=false -lock-timeout=5m -out=next.tfplan; echo "normal exit $?"
python3 driftwatch.py report --refresh-plan <(tofu show -json drift.tfplan) --plan <(tofu show -json next.tfplan) --stack <stack>
```

Use `terraform` in place of `tofu` on Terraform stacks. Exit 1 is an error, not drift: read it first (a held state lock means an apply is running; wait, do not force-unlock). Refresh-only exit 0 with normal exit 2 means there is no drift, only unapplied changes in the code: hand those to whoever owns the branch and stop here.

## 2. Classify every finding

| Finding | In the plans | First question |
|---|---|---|
| changed outside | refresh-only `# X has changed`; normal plan `~ update` | was the new value an intentional fix that should stay? |
| deleted outside | refresh-only `# X has been deleted`; normal plan `+ create` | is the object really gone, or did the provider decide it is a different object (`local_file` after an edit)? does it hold data? |
| replaced by the next apply | normal plan `-/+` with `# forces replacement` on a drifted attribute | what does a replacement destroy (disks, IPs, data), and who must agree before it happens? |
| ignored | refresh-only reports it; normal plan is a no-op for the address | is the attribute listed in `ignore_changes` on purpose, or is it just not set in the code? |
| unmanaged | not in either plan | does a scan of the account or cluster show objects with your tags or names that are not in `tofu state list`? |

Mask every value marked sensitive before you write anything down. Treat computed churn (`resource_version`, `etag`, `last_modified`) as noise unless it is the only change.

## 3. Find out who changed it and when

```bash
aws cloudtrail lookup-events --lookup-attributes AttributeKey=ResourceName,AttributeValue=<name> --max-results 20
gcloud logging read 'protoPayload.resourceName:"<name>"' --freshness=7d --limit=20
az monitor activity-log list --resource-id <id> --offset 7d
kubectl -n <ns> get <kind> <name> -o yaml --show-managed-fields     # managedFields[].manager names the client, e.g. kubectl-patch or kubectl-edit
```

Name a person or a system and a time. An operator, an autoscaler, an admission webhook or a cloud default changing values on its own is a different decision from a human hot fix.

## 4. Decide, per resource

| Situation | Decision | How |
|---|---|---|
| unintended change, or a hot fix that is no longer needed | **revert** | review `tofu plan`, then apply the saved plan; the update puts the configured value back |
| intentional fix that should stay | **accept and codify** | write the new value into the code in a PR; the next plan is a no-op for the resource and the apply records it |
| another system owns the attribute (replica counts, webhook labels, tags a cost tool writes) | **ignore** | `lifecycle { ignore_changes = [<that attribute>] }`, never `all` on anything but a bootstrap object |
| deleted outside and still needed | **recreate** | if it held data, restore from backup first; check why deletion protection did not stop it; then apply |
| deleted outside and no longer needed | **forget** | remove the resource block from the code, then `tofu apply -refresh-only` to record its absence |
| replaced by the next apply | **block applies on this stack** | codify the drifted value if it is correct, or schedule the replacement explicitly; never let an unrelated PR carry it |
| created outside | **adopt** | an `import { to = ..., id = ... }` block and `tofu plan -generate-config-out=generated.tf` (experimental), then review and apply |

`tofu apply -refresh-only` on its own only accepts reality into state. Unless the code is changed to match, the next normal plan will want to revert it again.

## 5. Apply the decision safely

One stack at a time. Save the plan with `-out`, read it in full, apply that file (`tofu apply next.tfplan`), and confirm with a refresh-only plan that exits 0. If several findings need different decisions, codify and ignore in one PR, revert in the apply of that PR, and keep replacements out of it.

## 6. Report

Write in the drift issue or the incident: the stack; counts per category; per finding the address, the attribute paths that changed (no sensitive values), who changed it and when, the decision and the PR or apply that carried it out; any finding left open and why; and whether the next nightly run is expected to be clean. Close the issue only after a refresh-only plan exits 0.
