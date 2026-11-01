# Infrastructure as Code

*A state file and a plan/apply engine, then Terraform, OpenTofu, Pulumi, Crossplane and Ansible.*

Infrastructure as code is often taught as syntax: HCL blocks, YAML playbooks, provider arguments. The syntax is the easy part. The hard part, and the source of every outage caused by an IaC tool, is the engine underneath: how it knows what exists, how it decides what to change, what it records, and what happens when two people run it at once. This phase builds that engine first, in Python, over local files, so that every plan you read afterwards, from any tool, is something you could have produced by hand.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| [Plan and Apply](01-state-reconciler/docs/en.md) | Build | a provider, a state file, refresh, diff, plan symbols, apply | `terraform` / `tofu` `init`, `plan`, `apply`, `state`, `show` |
| [Dependency Graph](02-dependency-graph/docs/en.md) | Build | `minigraph`: a DAG from references and `depends_on` in `.tf.json`, cycle paths, waves, a parallel walk, reversed destroy, `create_before_destroy` and `(known after apply)` | `tofu graph`, `-parallelism`, `depends_on`, `lifecycle` |
| [Modules & Variables](03-modules-variables/docs/en.md) | Build | `minimod`: modules from `.tf.json`, typed and validated variables with OpenTofu's input precedence, locals, sensitive outputs, `count`/`for_each` addresses and `moved` blocks | module installation, `-var` / `.tfvars`, `tofu console`, `state list`, `moved {}` |
| [Remote State & Locking](04-remote-state-locking/docs/en.md) | Build | `statesrv`: the HTTP backend protocol with atomic locks, lineage and serial checks, version history, an audit log and force-unlock | `backend "http"`, two concurrent `tofu apply` runs, `force-unlock`, `state pull`/`push`, S3 `use_lockfile`, state encryption |
| [Drift Detection](05-drift-detection/docs/en.md) | Build | `driftwatch`: refresh-only plans classified into changed outside, deleted outside, replace on next apply and ignored, `-detailed-exitcode` semantics and a Markdown report with a dedupe key | `tofu plan -refresh-only -detailed-exitcode`, `apply -refresh-only`, `ignore_changes`, a nightly GitHub Actions job |
| [Policy as Code](06-policy-as-code/docs/en.md) | Build | `planguard`: declarative rules over `tofu show -json` (tags, values, sizes, no delete or replace, module sources) where unknown values fail closed, plus the same rules in Rego | `conftest`, `opa eval`, Sentinel, Checkov, Trivy |
| [Pulumi & Crossplane](07-pulumi-crossplane/docs/en.md) | Build | `minipulumi` (lazy outputs, a resource graph, preview/up/destroy that runs a real Pulumi program unchanged) and `minixp` (patch-and-transform composition rendering with readiness) | `pulumi preview`/`up` on a local backend, `crossplane composition render`, XRDs and Compositions |
| [Ansible](08-ansible-config/docs/en.md) | Build | `miniansible`: inventories, a Jinja subset, modules that check before they change, `--check --diff`, handlers and a PLAY RECAP | `ansible-playbook --check --diff`, `ansible-inventory --graph`, `-m setup`, ansible-lint, the docker connection |

Further lessons for this phase are proposed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md).

## Reference layer

The long-form guides, cheat sheet, certification prep and interview questions for this phase live under [Terraform](https://cloudcaptain.io/docs/tools/terraform/) and [Ansible](https://cloudcaptain.io/docs/tools/ansible/). The Terraform Associate (004) exam guide is the one to open once the first three lessons are done.
