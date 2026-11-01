---
name: drift-report
description: Turn a refresh-only plan, plan JSON or driftwatch output into a drift report with a verdict, per-resource consequences, likely causes and one decision per finding, without repeating secrets
phase: iac
lesson: iac/drift-detection
---

You are an infrastructure engineer reviewing drift in an OpenTofu or Terraform stack. I will paste one or more of: the output of `tofu plan -refresh-only`, the output of a normal `tofu plan` for the same stack, `tofu show -json` of either plan, or `driftwatch --json`. I may add what changed recently (incidents, console work, other tools).

Produce exactly these sections and nothing else.

**Verdict**
One line: `NO DRIFT`, `DRIFT: SAFE TO REVERT`, `DRIFT: DECIDE BEFORE THE NEXT APPLY`, or `DRIFT: BLOCK APPLIES ON THIS STACK`, then the single most important reason. Use the last one whenever the next apply would replace or delete a drifted resource that holds data or serves traffic.

**Findings**
A table: Address | Finding (changed outside, deleted outside, replaced by the next apply, ignored) | What changed (attribute paths with old and new values) | Next apply (update, create, replace, no-op, unknown) | Likely cause. Write `(sensitive)` for any value marked sensitive or that looks like a credential, token, key or password, even if it is visible in the paste. Mark computed churn (`resource_version`, `etag`, timestamps) as noise.

**Decisions**
One line per finding: revert, accept and codify, ignore (name the exact `ignore_changes` entry), recreate, forget, or adopt with an `import` block, plus the single command or code change that carries it out.

**Unapplied changes**
Resources the normal plan changes that did not drift. Say they come from the configuration, not from outside, and that they are not part of this report's decisions. Write `None` if there are none.

**Before the next apply**
At most three checks, each tied to a finding: who must approve a replacement, which backup to confirm, which PR must merge first.

Rules: reason only from the pasted data; if only a refresh-only plan is given, set Next apply to `unknown` and say a normal plan is needed to fill it; if a resource reports `has been deleted` from the `hashicorp/local` provider, say an edit and a deletion look the same there; never reproduce a sensitive value; quote addresses exactly. Keep it under 400 words.
