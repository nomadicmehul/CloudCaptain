# Infrastructure as Code

*A state file and a plan/apply engine, then Terraform, OpenTofu, Pulumi, Crossplane and Ansible.*

Infrastructure as code is often taught as syntax: HCL blocks, YAML playbooks, provider arguments. The syntax is the easy part. The hard part, and the source of every outage caused by an IaC tool, is the engine underneath: how it knows what exists, how it decides what to change, what it records, and what happens when two people run it at once. This phase builds that engine first, in Python, over local files, so that every plan you read afterwards, from any tool, is something you could have produced by hand.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| Plan and Apply | Build | a provider, a state file, refresh, diff, plan symbols, apply | `terraform` / `tofu` `init`, `plan`, `apply`, `state`, `show` |

More lessons in this phase are listed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md): dependency graphs and parallel apply, modules and variables, remote state and locking, drift detection in CI, policy as code (OPA and Sentinel), Pulumi and Crossplane as alternatives, and configuration management with Ansible where mutable servers still exist.

## Reference layer

The long-form guides, cheat sheet, certification prep and interview questions for this phase live under [Terraform](https://cloudcaptain.io/docs/tools/terraform/) and [Ansible](https://cloudcaptain.io/docs/tools/ansible/). The Terraform Associate (004) exam guide is the one to open once the first three lessons are done.
