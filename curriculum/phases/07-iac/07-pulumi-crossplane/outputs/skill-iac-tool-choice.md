---
name: iac-tool-choice
description: Decide between OpenTofu or Terraform, Pulumi and Crossplane for a workload by looking at who changes it, how drift must be handled and where the state should live, then prove the choice with a one-hour spike in each candidate
version: 1.0.0
phase: iac
lesson: iac/pulumi-crossplane
tags: [iac, opentofu, terraform, pulumi, crossplane, platform-engineering, decision]
---

# IaC tool choice

Use this when a team asks "should we use Terraform, Pulumi or Crossplane", when a platform team wants to offer self-service infrastructure, or when a migration from one tool to another is proposed. The three tools run the same engine (desired state, a diff against a record of what exists, providers that act); choose by the front end and by when the engine runs, not by syntax preference.

## 1. Collect the facts before any opinion

```bash
find . -name '*.tf' -o -name '*.tf.json' | wc -l                       # existing HCL estate
grep -rhoE 'backend "[a-z0-9]+"' --include='*.tf' . | sort | uniq -c   # where state lives today
grep -rhoE '^\s*provider "[a-z0-9_-]+"' --include='*.tf' . | sort | uniq -c
ls Pulumi.yaml Pulumi.*.yaml 2>/dev/null                                # any Pulumi projects
kubectl api-resources --api-group=apiextensions.crossplane.io 2>/dev/null   # Crossplane installed?
kubectl get providers.pkg.crossplane.io,functions.pkg.crossplane.io 2>/dev/null
```

Then ask, and write the answers down:

- Who changes this infrastructure: one platform team, many application teams, or a machine (an operator, a pipeline)?
- How often, and does every change need a human to read a plan first?
- What happens today when someone changes it by hand (drift): ignored, found by a nightly plan, or must be reverted within minutes?
- Is there a Kubernetes cluster that is already treated as production, with on-call?
- Which languages does the team already test and review well?
- Which providers are needed, and does each tool have them at the needed depth?

## 2. Decide with the table

| Signal | Points to | Why |
|---|---|---|
| One team, reviewed plans gate every change, large existing HCL estate | OpenTofu (or Terraform where the BUSL 1.1 licence is acceptable) | The plan file is the review artifact; nothing to migrate. |
| Logic the HCL expression language fights: loops over API results, shared libraries, unit tests of infrastructure code | Pulumi | Real language, same plan/apply and state model; `pulumi preview` is the plan. |
| Infrastructure embedded in another program (a SaaS that creates a stack per customer) | Pulumi Automation API, or OpenTofu driven by a job runner | The engine is called as a library instead of a CLI. |
| Many teams request the same shapes (a database, a bucket, an app) through Kubernetes and GitOps | Crossplane: an XRD as the API, a Composition as the implementation | RBAC, namespaces and Argo CD or Flux become the request workflow. |
| Drift must be reverted continuously without anyone running anything | Crossplane | Providers reconcile managed resources forever. |
| A change must never happen without a human reading it first | OpenTofu or Pulumi in CI with a saved plan | Crossplane has no plan step to approve; review moves to the pull request that changes the XR or Composition. |
| No production Kubernetes cluster, or nobody on call for one | OpenTofu or Pulumi | Crossplane makes the cluster part of every infrastructure change. |
| Mutable servers, packages and files on hosts | Ansible (see the ansible-config lesson) | None of the three manages what is inside a VM well. |

Mixed answers are normal: OpenTofu for the foundation (accounts, networks, the cluster that runs Crossplane), Crossplane for per-team resources, Pulumi where a program owns the lifecycle. Write down which tool owns which resources; two tools must never manage the same resource.

## 3. Prove it with a one-hour spike per candidate

Use the same small target in each tool (for example one bucket with a lifecycle rule and one IAM policy that references it).

```bash
tofu init && tofu plan -out=plan.out && tofu show -json plan.out > plan.json        # OpenTofu: reviewable plan
pulumi new python --yes && pulumi preview --diff && pulumi up --yes                   # Pulumi: same plan semantics, in Python
pulumi stack export | python3 -c 'import json,sys; [print(r["urn"].split("::")[-1], [d.split("::")[-1] for d in r.get("dependencies", [])]) for r in json.load(sys.stdin)["deployment"]["resources"]]'
crossplane composition render xr.yaml composition.yaml functions.yaml                 # Crossplane: render offline, needs Docker
crossplane composition render xr.yaml composition.yaml functions.yaml --observed-resources observed.yaml
```

For each spike record: lines of code, how the plan or render shows a replacement, how a secret is stored (Terraform and OpenTofu state hold sensitive values in plaintext unless OpenTofu state encryption is on; Pulumi encrypts secret values per stack; Crossplane writes connection details to Kubernetes Secrets), what a manual change looks like on the next run, and how long a newcomer needed to read it.

## 4. Plan the migration, if there is one

| From → to | Move the code | Move the ownership |
|---|---|---|
| HCL → Pulumi | `pulumi convert --from terraform --language python --out ./pulumi` as a first draft, then review it like new code | `pulumi import TYPE NAME ID` per resource (or an import file); the first `pulumi preview` after import must show no changes |
| HCL → Crossplane | Write an XRD for the shape teams ask for and a Composition that renders the managed resources | Create the managed resources with `crossplane.io/external-name` set to the existing cloud id, start with an observe-only management policy, then remove the resource from OpenTofu state with `tofu state rm` |
| Pulumi → HCL | Rewrite; there is no converter worth trusting | `tofu import` per resource, then `pulumi state delete URN` so Pulumi forgets it without deleting it |

Never delete from the old tool's state before the new tool shows a no-op plan for the same resource.

## 5. Report

Write: the facts from step 1; the decision table rows that applied and the tool chosen per resource group; the spike results side by side (lines of code, replacement display, secret handling, drift behaviour); who owns which resources after the change; the migration steps with the no-op check after each; and the risks in order: double management during migration, a control plane without on-call, secrets in state, licence terms. If the answer is "keep what we have", say which signal decided it.
