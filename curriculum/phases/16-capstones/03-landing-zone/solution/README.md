# A Landing Zone You Can Prove: Multi-Cloud IaC With Guardrails: reference solution

Finish the capstone before reading this. CI grades this folder on every change
to prove the grader can be passed with full marks.

| Path | Milestone | What it does |
|---|---|---|
| `modules/network/` | module-structure, network-baseline | VPC, private and public subnets per zone (`cidrsubnet(cidr, 4, i)` and `8 + i`), internet gateway for the public route table only, closed default security group, load-balancer group (443 from anywhere, tagged `role = "load-balancer"`), app group, flow log to S3 |
| `modules/identity/` | module-structure, identity-guardrails | guardrail policy (deny outside `allowed_regions` except global services, deny account-level actions), boundary built from the guardrail plus the maximum allow list, deploy and read-only roles with the boundary and the guardrail attached |
| `modules/baseline/` | module-structure, policy-gate | log archive bucket: public access block, SSE, versioning, owner-enforced, lifecycle expiry |
| `envs/{hub,dev,prod}/` | all AWS milestones | thin roots: tags, the three modules, `providers.tf` pointed at `var.aws_endpoint`, `backend "s3"` with `use_lockfile = true`; the hub also peers with every spoke it finds and adds routes on both sides |
| `modules/network-gcp/`, `modules/network-azure/` | multi-cloud | the same inputs and outputs on Google Cloud and Azure, each with `tests/network.tftest.hcl` (`mock_provider`, four runs) |
| `policy/gate.py`, `policy/landing-zone.json` | policy-gate | one function per rule over the plan JSON; companions of a bucket matched through `configuration` references within the same module instance, or by bucket name |
| `bin/lz`, `tools/lzcli/` | policy-gate, state-drift | `cli.py` (subcommands and JSON output), `tofu.py` (init with a generated `-backend-config` for the local endpoint, plan, apply, lock errors to exit 3), `drift.py` (`resource_drift` to attribute paths), `aws.py` (local-endpoint check and a SigV4-signed `PUT` that creates the state bucket on moto) |
| `tests/` | | unit tests for the policy rules, drift paths, lock handling and the CLI, with fake runners: `python3 -m unittest discover -s solution/tests` |

Every `.terraform.lock.hcl` pins the provider release and lists the `zh:` checksums of all its platforms, so `tofu init` verifies whatever it downloads.
