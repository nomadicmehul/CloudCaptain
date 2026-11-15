---
name: landing-zone-review
description: Review a change to an OpenTofu landing zone (network, identity, account baseline) by running the plan, the policy gate, an IAM evaluation of every role and a drift report, then decide ship, fix or escalate
version: 1.0.0
phase: capstones
lesson: capstones/landing-zone
tags: [opentofu, landing-zone, aws, iam, policy-as-code, drift, moto, review]
---

# Landing zone change review

Use this when a pull request touches `modules/`, `envs/` or `policy/` of a landing zone repository, when someone asks "is it safe to apply this to prod", or before a scheduled apply. The review runs against a local emulator first and a real account only after every step below is green. It assumes the `bin/lz` interface: `plan <env> --json`, `apply <env>`, `policy <plan.json> --json`, `drift <env> --json`.

## 1. Read the change, not just the diff

```bash
git diff --stat origin/main...HEAD -- modules envs policy
git diff origin/main...HEAD -- modules/*/variables.tf modules/*/outputs.tf
```

List every changed module input and output. A renamed or removed output breaks every env that reads it; a new required variable breaks every caller. A change to `policy/` changes what "compliant" means for every other change: review it as its own risk.

## 2. Format, validate and test every folder

```bash
tofu fmt -check -recursive modules envs
for d in modules/* envs/*; do (cd "$d" && tofu init -backend=false -input=false >/dev/null && tofu validate) || echo "FAILED $d"; done
for d in modules/network-gcp modules/network-azure; do (cd "$d" && tofu test); done
```

Any failure here stops the review: report it with the folder and the first diagnostic.

## 3. Plan every env against the emulator and gate the plans

```bash
docker run -d --rm --name lz-review-moto -p 127.0.0.1:5000:5000 motoserver/moto:5.2.5
export LZ_AWS_ENDPOINT=http://127.0.0.1:5000
for env in dev prod hub; do bin/lz apply "$env" || echo "APPLY FAILED $env"; done
for env in hub dev prod; do bin/lz plan "$env" --json > "/tmp/$env.plan.summary.json"; done
for env in hub dev prod; do bin/lz policy "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plan_json"])' /tmp/$env.plan.summary.json)" --json; done
```

Apply first so the second plan shows what this change would do to an existing landing zone, not a greenfield one. Count replacements (`["delete","create"]`) in each plan JSON: a replaced VPC, subnet or bucket is an outage or data loss and needs a migration plan, whatever the policy says.

## 4. Evaluate every role the change touches

```bash
aws --endpoint-url "$LZ_AWS_ENDPOINT" iam get-account-authorization-details --filter Role LocalManagedPolicy > /tmp/iam.json
```

For each `lz-<env>-*` role, decide these requests with an IAM evaluator (explicit deny, identity allow, permissions boundary): a write in the home region (deploy: allowed; read-only: denied), the same write in another region (explicit deny), `iam:CreateUser`, `iam:DeleteRolePermissionsBoundary`, `organizations:LeaveOrganization` (all denied, the last two explicitly). Flag any Allow with `Action: "*"` on `Resource: "*"`, any `NotAction` Allow, and any role without a boundary.

## 5. Check for drift before you apply

```bash
for env in hub dev prod; do bin/lz drift "$env" --json; done
```

Drift on the real account means someone changed it by hand. Applying now reverts that change silently. Find out why before you overwrite it.

## 6. Decide

| Finding | Decision |
|---|---|
| fmt, validate or tests fail | fix: send back with the first diagnostic |
| `lz policy` reports any violation | fix, or change the rule in its own reviewed PR |
| a VPC, subnet, bucket or role is replaced | escalate: needs a migration plan and an owner's sign-off |
| a role gains a broad Allow, loses its boundary, or an explicit deny becomes allowed | escalate to security |
| an output or required variable changed | fix callers in the same PR, or keep the old name with a deprecation note |
| drift on the target env | stop: reconcile or codify the manual change first |
| everything green on the emulator | ship to a sandbox account, then prod with the saved plan |

## Report

Write a short report with: the envs and modules touched; per env the plan counts (create, update, delete, replace) and the policy verdict; the role decisions that changed; drift found; the decision from the table and the one action that unblocks it. Quote rule IDs, resource addresses and role names exactly. State that the emulator does not evaluate IAM, SCPs or routing, so the sandbox run is still required.
