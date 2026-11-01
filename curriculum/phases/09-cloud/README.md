# Cloud Providers

*IAM, networking and compute mental models built by hand, then AWS, Azure and GCP services and certifications.*

Every cloud console is a thin layer over a few engines you can rebuild: a policy evaluator that decides every API call, route tables and stateful and stateless rules that decide every packet, a signing scheme that authenticates every request, a runtime that starts your function, a reconciler that keeps a fleet at its desired size, and a bill that joins usage to a price list. This phase builds each engine in Python, checks it against an emulator of the real API (no cloud account needed), and then names the same knobs in AWS first, with the Azure and GCP equivalents alongside, so certification questions and production incidents read as cases you have already evaluated.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| [IAM Policy Evaluation](01-iam-policy-evaluator/docs/en.md) | Build | `iameval`: AWS's evaluation order (explicit deny, RCPs and SCPs, resource policies, identity policies, boundaries, session policies), every condition operator, policy variables, cross-account rules and the exact AccessDenied text | `get-account-authorization-details`, `simulate-custom-policy`, Access Analyzer validation, CloudTrail AccessDenied events, Azure RBAC and GCP IAM |
| [VPC Reachability](02-vpc-reachability/docs/en.md) | Build | `vpcpath`: longest-prefix routing through IGW, NAT, peering, transit gateways and endpoints, stateful security groups and ordered NACLs, return paths with real ephemeral ports | `describe-route-tables`, `describe-security-groups`, Reachability Analyzer codes, flow logs, Azure NSGs and GCP firewall rules |
| [S3 & Request Signing](03-s3-signing/docs/en.md) | Build | `minis3`: Signature Version 4 from the spec (headers and presigned URLs) checked against AWS's test vectors, and an S3-compatible server with listing, versioning, multipart ETags and bucket policies | `aws s3api`, `aws s3 presign`, SignatureDoesNotMatch, Azure SAS and GCS signed URLs |
| [Lambda Runtime](04-lambda-runtime/docs/en.md) | Build | `minilambda`: the Invoke and Runtime APIs, execution environments with init, freeze and timeouts, concurrency and throttles, `REPORT` lines, and a Python bootstrap that also runs under AWS's emulator | `aws-lambda-rie` with the AWS Python image, `aws lambda invoke`, provisioned concurrency, SnapStart |
| [Auto Scaling Groups](05-autoscaling-groups/docs/en.md) | Build | `miniasg`: zone balancing, EC2 and ELB health checks, replacement, target tracking, step and simple scaling, lifecycle hooks and instance refresh on a virtual clock | `aws autoscaling` against an emulator, `describe-scaling-activities`, Karpenter, VM Scale Sets and managed instance groups |
| [FinOps with FOCUS](06-finops-focus/docs/en.md) | Build | `billjoin`: FOCUS 1.4 rows and CUR 2.0 mapped onto them, tag allocation that reconciles to the cent, commitment utilisation and coverage, unit costs, anomalies and showback | Cost and Usage Reports, FOCUS exports from AWS, Azure and GCP, Cost Explorer, DuckDB over the export |
| [Security Posture](07-security-posture/docs/en.md) | Build | `posture`: read-only collection and 27 checks (public buckets, open admin ports, encryption, MFA, keys, logging) with evidence, fixes and CIS and Security Hub references | Prowler, Security Hub, AWS Config, Checkov on Terraform, Defender for Cloud and Security Command Center |

Further lessons for this phase are proposed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md).

## Reference layer

The long-form guides, cheat sheets, exam prep and interview questions live under [AWS](https://cloudcaptain.io/docs/cloud/aws), [Azure](https://cloudcaptain.io/docs/cloud/azure) and [GCP](https://cloudcaptain.io/docs/cloud/gcp). The AWS exam prep page is the one to open once the IAM and VPC lessons are done.
