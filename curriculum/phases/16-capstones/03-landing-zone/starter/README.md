# A Landing Zone You Can Prove: Multi-Cloud IaC With Guardrails: starter

Copy this folder into a new repository and build the capstone there. The full brief, the architecture and every milestone are in the capstone page (`docs/en.md` next to this folder, or cloudcaptain.io/learn/capstones/landing-zone).

## What is already here

| Path | What it gives you |
|---|---|
| `bin/lz` | the command-line entry point the grader calls; every subcommand exists and fails with `not implemented: milestone ...`. It already contains `tofu_environment()`, which refuses a non-local `LZ_AWS_ENDPOINT` and turns it into `TF_VAR_aws_endpoint` and fake credentials. Rewrite it in any language you like, as long as the interface below holds. |
| `envs/hub`, `envs/dev`, `envs/prod` | `providers.tf` with the AWS provider pointed at `var.aws_endpoint` (moto) when it is set, and at real AWS when it is null; `versions.tf` pinning `hashicorp/aws` 6.73.0; `.terraform.lock.hcl` with the checksums of that release. You add `main.tf`, the remaining variables, `terraform.tfvars`, outputs and the `backend "s3"` block. |
| `.gitignore` | keeps `.terraform/`, `.lz/`, state and plan files out of git |

Nothing under `modules/` or `policy/` exists yet: those are the milestones.

## The interface the grader drives

| Command | Must |
|---|---|
| `bin/lz plan <env> --json` | plan `envs/<env>`, write `tofu show -json` of the plan to a file, print `{"env": "<env>", "plan_json": "<path>"}`, exit 0; exit 3 with `{"error": "state-locked", ...}` when the state is locked |
| `bin/lz apply <env>` | apply `envs/<env>` without prompting, exit 0; exit 3 when locked |
| `bin/lz policy <plan.json> --json` | print `{"violations": [{"rule": "...", "address": "...", "message": "..."}]}`; exit 1 on violations, 0 when clean, 2 on bad input |
| `bin/lz drift <env> --json` | print `{"env": "<env>", "drifted": true, "resources": [{"address", "type", "id", "attributes": ["tags.owner", ...]}]}`; exit 0 clean, 2 on drift |

`LZ_AWS_ENDPOINT` (set by the grader to `http://127.0.0.1:<port>`) must reach every AWS provider and the S3 backend. Fixed names: region `eu-central-1`; tags `owner`, `env` (= folder name) and `cost-center` on everything taggable; roles `lz-<env>-deploy` and `lz-<env>-readonly`; the load-balancer security group tagged `role = "load-balancer"`; all ranges inside `10.0.0.0/8`.

Module interface shared by `modules/network`, `modules/network-gcp` and `modules/network-azure`:

- inputs: `name`, `env`, `region`, `cidr_block`, `zones`, `tags`, `flow_log_destination`
- outputs: `network_id`, `cidr_block`, `private_subnet_ids`, `private_subnet_cidrs` (`cidrsubnet(cidr_block, 4, i)`), `public_subnet_ids`, `public_subnet_cidrs` (`cidrsubnet(cidr_block, 4, 8 + i)`), `public_ingress_ports` (`[443]`)

Policy rule IDs: `required-tags`, `no-public-s3`, `no-open-ingress`, `encryption`, `cidr-allocation`.

## Milestones

| # | Milestone | Points | You build |
|---|---|---|---|
| 1 | `module-structure` | 2 | `modules/network`, `modules/identity`, `modules/baseline` with typed, described, validated variables; env roots that pass `tofu fmt -check` and `tofu validate` |
| 2 | `network-baseline` | 3 | hub, dev and prod VPCs, private subnets in two zones, only 443 public on load balancers, flow logs, hub peered with both spokes |
| 3 | `identity-guardrails` | 3 | deploy and read-only roles with boundaries, least privilege, explicit denies for other regions and account actions |
| 4 | `policy-gate` | 3 | `bin/lz plan` and `bin/lz policy` with the five rules |
| 5 | `multi-cloud` | 2 | `modules/network-gcp` and `modules/network-azure` with the same interface and `tofu test` suites using `mock_provider` |
| 6 | `state-drift` | 2 | S3 backend with locking (`use_lockfile = true` or DynamoDB), `bin/lz drift` |

Pass at 11 of 15.

## Grade it

You need `tofu` (1.10 or newer), `docker` and `python3`. From a clone of CloudCaptain:

```bash
bash <cloudcaptain>/curriculum/phases/16-capstones/03-landing-zone/grader/grade.sh .
bash <cloudcaptain>/curriculum/phases/16-capstones/03-landing-zone/grader/grade.sh . --milestone network-baseline
```

The grader starts moto in Docker on `127.0.0.1`, works on a copy of your project, and removes everything it created. To grade every push, copy `grader/workflow.yml` from the capstone to `.github/workflows/cloudcaptain-grade.yml`.

To try your code by hand against moto:

```bash
docker run -d --rm --name lz-moto -p 127.0.0.1:5000:5000 motoserver/moto:5.2.5
export LZ_AWS_ENDPOINT=http://127.0.0.1:5000
bin/lz apply dev
```
