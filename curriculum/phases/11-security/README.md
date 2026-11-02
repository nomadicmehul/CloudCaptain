# Security & DevSecOps

*A policy engine and a secrets workflow, then OPA, Kyverno, Falco, Vault and cloud security posture.*

Security work in operations is mostly the same few mechanisms applied at different layers: something that holds a secret and hands out short-lived leases, something that says no before an object exists, something that watches system calls after it runs, something that decides which pod may talk to which, and something that turns a scanner's thousand findings into the five that matter. Earlier phases already built some of them (capabilities and seccomp, RBAC, IAM evaluation, workflow and supply-chain security). This phase builds the rest from the standard library and checks each against the tool teams run in production.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| [Admission](02-admission-policy/docs/en.md) | Build | `miniadmit`: a validating and mutating webhook, policies as data, a CEL subset like ValidatingAdmissionPolicy | ValidatingAdmissionPolicy, Kyverno, Gatekeeper |
| [Runtime detection](03-runtime-detection/docs/en.md) | Build | `minifalco`: events from `strace`, a Falco-style rule language, detections and rule tuning | Falco with the modern eBPF driver, Tetragon |
| [NetworkPolicy](04-network-policy/docs/en.md) | Build | `mininetpol`: the NetworkPolicy evaluator, a connectivity matrix and `explain` | NetworkPolicy on kind, Cilium and Calico, AdminNetworkPolicy |
| [Secret scanning](05-secret-scanning/docs/en.md) | Build | `minileaks`: provider rules, entropy, Git history, allowlists, baselines and a pre-commit mode | gitleaks, push protection, `git filter-repo` |
| [Workload identity](06-workload-identity/docs/en.md) | Build | `miniid`: SPIFFE IDs, X.509 SVIDs from a small CA, peer attestation, rotation and mutual TLS authorised by ID | SPIRE, service meshes, csi-driver-spiffe |
| [Benchmarks](08-cis-benchmark/docs/en.md) | Build | `minibench`: CIS Kubernetes checks as data against a node, with evidence and reports | kube-bench, kubescape |

More lessons for this phase are being written; the plan is in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md).

## Reference layer

The long-form guides, cheat sheet and interview questions for this phase live under [DevSecOps](https://cloudcaptain.io/docs/tools/devsecops) and [Cloud Security](https://cloudcaptain.io/docs/cloud/cloud-security). The CKS exam-prep guide maps several of these lessons onto its domains.
