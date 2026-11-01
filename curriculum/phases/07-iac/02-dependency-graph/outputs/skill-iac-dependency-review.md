---
name: iac-dependency-review
description: Review a Terraform or OpenTofu change for ordering problems before it is applied: hidden runtime dependencies, needless depends_on, cycles, destroy-first replacements, destroy order and parallelism
version: 1.0.0
phase: iac
lesson: iac/dependency-graph
tags: [terraform, opentofu, iac, dependency-graph, depends_on, create_before_destroy, review]
---

# IaC dependency review

Use this when a pull request adds or rewires resources, adds `depends_on` or `lifecycle` blocks, when a plan fails with `Error: Cycle`, when an apply works only the second time, or when someone wants to change `-parallelism`. Work from the real graph and a real plan, not from reading the diff.

## 1. Get the graph and a plan

```bash
tofu init -input=false
tofu graph > graph.dot                         # the plan graph; edges point from a node to what it needs
tofu graph -type=plan-destroy > destroy.dot
tofu plan -input=false -out=plan.out && tofu show -json plan.out > plan.json
dot -Tsvg graph.dot -o graph.svg               # optional, needs graphviz
```

Use `terraform` in place of `tofu` on Terraform projects. If the plan cannot run (no credentials), say so and review `graph.dot` alone; `tofu graph` needs `init` but no cloud access.

## 2. Turn the graph into waves

Resource-to-resource dependencies, looking through locals, variables, modules and provider nodes, grouped into waves:

```bash
python3 - graph.dot <<'PY'
import re, sys
Q = r'"((?:[^"\\]|\\.)*)"'
SKIP = {"local", "var", "output", "module", "provider", "meta", "root", "path", "terraform"}
def resource(raw):
    bare = re.sub(r" \([^)]*\)$", "", raw.replace("[root] ", "", 1))
    inner = re.sub(r"^(module\.[\w-]+\.)+", "", bare)
    ok = re.match(r"^(data\.)?[a-z][a-z0-9_]*\.[\w-]+$", inner) and inner.split(".")[0] not in SKIP
    return bare if ok else None
adj = {}
for line in open(sys.argv[1]):
    edge, node = re.match(r"\s*" + Q + r"\s*->\s*" + Q, line), re.match(r"\s*" + Q, line)
    if edge:
        a, b = (resource(x) or x for x in edge.groups())
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set())
    elif node:
        adj.setdefault(resource(node.group(1)) or node.group(1), set())
def needs(n, seen):
    out = set()
    for d in adj[n] - seen:
        seen.add(d)
        out |= {d} if resource(d) else needs(d, seen)
    return out
deps = {n: needs(n, {n}) for n in adj if resource(n)}
wave = 0
while deps:
    ready = sorted(n for n, d in deps.items() if not d)
    if not ready:
        sys.exit(f"cycle among: {', '.join(sorted(deps))}")
    wave += 1
    print(f"wave {wave}: {', '.join(ready)}")
    deps = {n: d - set(ready) for n, d in deps.items() if n not in ready}
PY
```

Record the number of waves, the longest chain (the critical path) and the resources with the most dependents (hubs: a VPC, an IAM role, a KMS key). A change to a hub touches everything behind it. A `cycle among:` exit goes to step 5.

## 3. Hunt hidden dependencies

An edge exists only where an argument references another resource or `depends_on` lists it. For every resource the PR adds, ask what must be true at runtime for it to work on its first boot, and check that each of those has an edge.

| The resource | needs at runtime | missing edge looks like | fix |
|---|---|---|---|
| instance, function or task with an IAM role | the policy attachment on that role | references the role or instance profile, not `aws_iam_role_policy_attachment` / `aws_iam_role_policy` | `depends_on` the attachment; add a retry, IAM is eventually consistent |
| workload in a private subnet that downloads on boot | NAT gateway, route or VPC endpoint | references the subnet only | `depends_on` the route or endpoint |
| any GCP resource | the API enabled by `google_project_service` | references the project id string, not the service resource | reference the service resource or `depends_on` it |
| custom resources (`kubernetes_manifest`) | the CRD installed by `helm_release` or another manifest | no reference to the release | `depends_on`, and often a separate apply, because the CRD must exist at plan time |
| an encrypted volume, bucket or database | the KMS key policy or grant | references the key arn only | `depends_on` the policy or grant resource |
| certificate consumers (listeners, CDN) | a validated certificate | references `aws_acm_certificate.x.arn` | reference `aws_acm_certificate_validation.x.certificate_arn` instead: a real reference beats `depends_on` |
| DNS-dependent bootstrap (join a cluster by name) | the record exists and resolves | references the zone, not the record | reference the record's `fqdn` |

Prefer a real reference over `depends_on` whenever the needed resource exports a value you can pass through.

## 4. Audit every depends_on

For each explicit edge in the diff:

- **Redundant**: the resource already references the target (directly or through a local or module output). Remove it; it hides intent.
- **On a module or data source**: every data source inside waits until apply whenever the dependency has changes, so more values become `(known after apply)` and plans get vaguer. Narrow it to the one resource that needs the order.
- **Justified**: a hidden dependency from step 3. Ask for a comment in the PR (or the code) naming the runtime reason.

## 5. Read and break cycles

`Error: Cycle: a, b, c` names every resource in the loop. `depends_on` never fixes a cycle; it can only add edges.

| Shape | Typical cause | Break it by |
|---|---|---|
| two security groups | inline ingress rules reference each other | separate `aws_vpc_security_group_ingress_rule` resources that reference both groups |
| resource and its own policy | a bucket policy embedded in the bucket that names a role that names the bucket | a separate policy resource |
| module A output into module B and back | a value passed in both directions | move the shared value to the root as a local or variable both read |
| `create_before_destroy` mixed with a dependent replaced first | rare after propagation; usually a provisioner or `replace_triggered_by` loop | remove the trigger, or split the replacement over two applies |

Re-run `tofu validate` after the change; it builds the same graph.

## 6. Replacements and ordering

From `plan.json`, list `resource_changes[]` whose `change.actions` is `["delete","create"]` (destroy first) or `["create","delete"]` (create first). For each destroy-first replacement that other resources depend on (a hub from step 2), the dependents point at nothing between the two steps: that is downtime. Ask for `lifecycle { create_before_destroy = true }` and check the resource can exist twice at once: unique `name` fields collide, so expect `name_prefix` or a random suffix. Note that the flag propagates to everything the resource depends on.

## 7. Destroy order and parallelism

Check `destroy.dot` (or `tofu plan -destroy`) for resources whose deletion fails while something still uses them: subnets with ENIs left by load balancers or Lambda functions, security groups attached to instances outside the configuration, buckets with objects. These are dependencies outside the graph; fix them in code (`force_destroy`, ordering edges) rather than in the runbook.

`-parallelism` is a worker pool size (default 10). Lowering it never fixes an ordering bug; it hides races and multiplies apply time. Accept a change only with a measured rate-limit error, and prefer the provider's retry settings.

## 8. Report

Post: the number of resources, waves and the critical path; every hidden dependency found with the fix; each `depends_on` marked redundant, too broad or justified; any cycle with the edge to remove; destroy-first replacements of hubs with the downtime window and the `create_before_destroy` recommendation; destroy-order risks; a parallelism verdict. End with `ORDER SAFE`, `ORDER FIXES NEEDED` or `CANNOT ASSESS` and the reason. Never approve a change whose graph you did not extract.
