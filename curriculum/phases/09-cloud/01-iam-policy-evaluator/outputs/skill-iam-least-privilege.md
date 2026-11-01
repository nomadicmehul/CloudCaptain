---
name: iam-least-privilege
description: Review what an AWS principal can do, or what an IAM or resource-policy change will grant, against what it actually uses; find over-broad grants, write the smallest statements, and prove the before and after with a simulator matrix
version: 1.0.0
phase: cloud
lesson: cloud/iam-policy-evaluator
tags: [aws, iam, least-privilege, security, policy-evaluation, access-analyzer, cloudtrail, review]
---

# IAM least-privilege review

Use this when a pull request adds or changes an IAM policy, a bucket, key, queue or trust policy, a permissions boundary or an SCP; when a workload asks for "more permissions" or someone proposes `AdministratorAccess` or `"Principal": "*"` to fix an AccessDenied; or during a periodic access review of roles and users.

## 1. Fix the principal and dump what applies

```bash
aws sts get-caller-identity                                        # which account and identity you are reviewing from
aws iam get-account-authorization-details > account.json           # users, groups, roles, managed policies, boundaries, tags, trust policies
PRINCIPAL=arn:aws:iam::ACCOUNT:role/NAME                           # or user/NAME; a running workload is a session of a role
aws iam get-role --role-name NAME --query 'Role.{Trust:AssumeRolePolicyDocument,Boundary:PermissionsBoundary,LastUsed:RoleLastUsed}'
aws organizations list-policies-for-target --target-id ACCOUNT --filter SERVICE_CONTROL_POLICY   # needs organization read access; repeat for each OU up to the root
```

Write down the principal exactly: an IAM user, or a role and the way it is assumed (by a service, by another account, by SSO). For each resource the workload touches, record the owning account and its resource-based policy (`aws s3api get-bucket-policy`, `aws kms get-key-policy`, `aws sqs get-queue-attributes --attribute-names Policy`, `aws secretsmanager get-resource-policy`). For a pull request, review the proposed documents as files next to the current ones.

## 2. Lint every document

```bash
aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY --policy-document file://policy.json --query 'findings[].[findingType,issueCode,findingDetails]' --output table
aws accessanalyzer validate-policy --policy-type RESOURCE_POLICY --validate-policy-resource-type AWS::S3::Bucket --policy-document file://bucket-policy.json
python3 iameval.py validate policy.json --type identity            # offline subset with the same finding types
```

Fix every `ERROR`. A `SECURITY_WARNING` needs a written justification or a change.

## 3. Flag over-broad grants

| Grant | Why it matters | Verdict |
|---|---|---|
| `"Action": "*"` or `"Resource": "*"` on write actions | covers every current and future action or resource | reject outside break-glass roles |
| Allow with `NotAction` or `NotResource` | grants everything not listed, including new services | reject; list the actions |
| `iam:PassRole` on `*` | can hand any role, admin included, to a service it controls | restrict to the role ARNs it passes, add `iam:PassedToService` |
| `iam:*`, `iam:Put*Policy`, `iam:Attach*Policy`, `iam:CreatePolicyVersion`, `iam:UpdateAssumeRolePolicy` | can grant itself anything | reject for workloads; boundary-protected delegation only |
| `sts:AssumeRole` on `*` | reaches every role whose trust policy names the account | list the roles |
| resource policy with `"Principal": "*"` and no condition | public, anonymous included | reject unless public by design and Block Public Access says so |
| trust policy naming another account with no `sts:ExternalId` or `aws:PrincipalOrgID` | confused-deputy risk | add the condition |
| `ForAllValues:...` in an Allow without `"Null": {key: "false"}` | true when the key is missing | add the Null check |
| `Version` missing or `2008-10-17` with `${...}` | variables are literal text | set `2012-10-17` |
| a `Principal` showing `AIDA...` or `AROA...` | the named user or role was deleted; the grant is dead | replace with the current ARN or remove |

## 4. Find what it actually uses

```bash
JOB=$(aws iam generate-service-last-accessed-details --arn $PRINCIPAL --granularity ACTION_LEVEL --query JobId --output text)
aws iam get-service-last-accessed-details --job-id $JOB --query 'ServicesLastAccessed[?TotalAuthenticatedEntities>`0`].[ServiceNamespace,LastAuthenticated]' --output table
aws cloudtrail lookup-events --lookup-attributes AttributeKey=Username,AttributeValue=NAME --max-results 50 --output json \
  | jq -r '.Events[].CloudTrailEvent | fromjson | [.eventSource, .eventName, (.errorCode // "ok")] | @tsv' | sort | uniq -c | sort -rn
```

Action-level last-accessed data covers a subset of services; CloudTrail `lookup-events` covers management events for 90 days, and object-level S3 or other data events only if a trail records them. Without either, run the workload in a test account with the candidate policy and collect the AccessDenied messages.

## 5. Write the smallest statements

One statement per resource family with explicit actions (no `*` in the action name unless the family is read-only and stable, such as `s3:Get*` on one bucket); resource ARNs down to the prefix (`arn:aws:s3:::bucket/app/*`); conditions for the context that must hold (`aws:SourceVpce`, `aws:PrincipalOrgID`, `aws:SecureTransport`, `s3:prefix`, `aws:ResourceTag/...`); a permissions boundary for any principal that may create other principals or policies. Keep explicit Deny statements for guardrails that must survive later Allows.

## 6. Prove it with a matrix

```bash
for a in s3:GetObject s3:PutObject s3:DeleteObject iam:PassRole sts:AssumeRole; do
  aws iam simulate-custom-policy --policy-input-list file://proposed.json --action-names $a --resource-arns arn:aws:s3:::bucket/app/x \
    --query 'EvaluationResults[0].[EvalActionName,EvalDecision]' --output text
done
aws iam simulate-principal-policy --policy-source-arn $PRINCIPAL --action-names s3:GetObject --resource-arns arn:aws:s3:::bucket/app/x \
  --resource-policy file://bucket-policy.json --resource-owner arn:aws:iam::ACCOUNT:root --context-entries ContextKeyName=aws:SecureTransport,ContextKeyValues=true,ContextKeyType=boolean
python3 iameval.py eval --principal $PRINCIPAL --auth-details account.json --policy proposed.json --action s3:GetObject --action s3:DeleteObject --resource arn:aws:s3:::bucket/app/x --json
```

Every action from step 4 must be `allowed` after the change; every grant flagged in step 3 must be `implicitDeny` or `explicitDeny` after, unless justified. The simulator does not evaluate RCPs or session policies; check those with `iameval` or in the policy text.

## 7. Check the other side and the gates

| Situation | What must also allow | Check |
|---|---|---|
| resource in another account | that account's resource-based policy, and its RCPs | `get-bucket-policy` etc. in the owning account names this principal |
| role assumed by a person or service | the role's trust policy | `get-role` trust names exactly the callers, with conditions |
| account in an organization | every SCP level from the root to the account | `list-policies-for-target` per level; the management account is exempt |
| principal with a permissions boundary | the boundary | the new actions are inside it, or the boundary change is reviewed too |
| sessions created with `--policy` or `--policy-arns` | the session policy | the caller that creates the session passes a policy that allows the actions |
| KMS-encrypted data | the key policy, which must allow explicitly | `aws kms get-key-policy --key-id KEY --policy-name default` |

## 8. Report

State: the principal and how it is assumed; every policy type that applies, with its source; the findings from step 2 and the grants from step 3 with the verdict for each; what the workload uses, with the evidence; the proposed statements; the before and after matrix; the other-side checks from step 7; and anything left wider than needed with an owner and a date. Do not approve a change that adds `*` actions on `*`, Allow with `NotAction`, unscoped `iam:PassRole`, a public principal without a condition, or `AdministratorAccess` to fix an AccessDenied whose message names a resource-based policy, SCP, boundary or session policy.
