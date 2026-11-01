---
name: ci-cloud-federation
description: Move a CI pipeline from stored cloud keys to OIDC federation (GitHub Actions or GitLab to AWS, Azure or Google Cloud) with pinned subjects and audiences, prove it from one job, delete the old keys, and triage a failing token exchange from its error code
version: 1.0.0
phase: cicd
lesson: cicd/ci-oidc
tags: [oidc, github-actions, gitlab, aws, sts, azure, gcp, workload-identity, ci-security, secrets]
---

# CI-to-cloud federation

Use this when a pipeline still holds long-lived cloud credentials (`AWS_ACCESS_KEY_ID`, a service principal secret, a service account key JSON), when a new pipeline needs cloud access, when a trust policy or federated credential changes in a pull request, or when `configure-aws-credentials`, `azure/login` or `google-github-actions/auth` fails.

## 1. Inventory the stored credentials

```bash
gh secret list --repo OWNER/REPO                                     # repository secrets; repeat with --org OWNER and per environment (--env NAME)
grep -rnE 'AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AZURE_CREDENTIALS|credentials_json|GOOGLE_CREDENTIALS' .github/workflows/
aws iam list-users --query 'Users[].UserName' --output text | tr '\t' '\n' | while read -r u; do
  aws iam list-access-keys --user-name "$u" --query 'AccessKeyMetadata[?Status==`Active`].[UserName,AccessKeyId,CreateDate]' --output text
done
aws iam get-access-key-last-used --access-key-id AKIA...           # last service, region and date the key was used
```

Record for every key: the IAM user, its policies, its age, the repositories and environments that hold it, and the last use. A key that nobody can trace to a pipeline is deactivated first, not migrated.

## 2. Design one identity per deployable thing

| Job | Subject to pin | Notes |
|---|---|---|
| deploy to an environment | `repo:ORG/REPO:environment:NAME` | protect the environment: required reviewers, deployment branches `main` only |
| publish from the default branch | `repo:ORG/REPO:ref:refs/heads/main` | no environment in the job, or the subject changes |
| release from tags | `repo:ORG/REPO:ref:refs/tags/v*` with `StringLike` | protect tags with a ruleset so only maintainers create them |
| plan or test on pull requests | `repo:ORG/REPO:pull_request` | read-only role only; the code is unreviewed |
| central deploy workflow for many repositories | custom template with `job_workflow_ref` (`include_claim_keys`) | the subject then names the reusable workflow and its ref |
| repository created after July 15, 2026, or opted in | `repo:ORG@ORG-ID/REPO@REPO-ID:...` | the immutable format; check which one the repository uses before writing the policy |

Never trust a whole issuer, a whole organization or `repo:ORG/REPO:*` for a role that can write.

## 3. Create the provider and the role

AWS (once per account, then one role per row above):

```bash
aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com --client-id-list sts.amazonaws.com
aws iam create-role --role-name deploy-prod --max-session-duration 3600 --assume-role-policy-document file://trust.json
aws iam attach-role-policy --role-name deploy-prod --policy-arn arn:aws:iam::ACCOUNT:policy/deploy-prod   # least privilege, written separately
```

`trust.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "arn:aws:iam::ACCOUNT:oidc-provider/token.actions.githubusercontent.com"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {"StringEquals": {
      "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub": "repo:ORG/REPO:environment:prod"
    }}
  }]
}
```

Azure: `az identity federated-credential create --name NAME --identity-name IDENTITY --resource-group RG --issuer https://token.actions.githubusercontent.com --subject repo:ORG/REPO:environment:prod --audiences api://AzureADTokenExchange`, then a role assignment for the identity at the narrowest scope.

Google Cloud: a pool provider with `--issuer-uri=https://token.actions.githubusercontent.com`, an `--attribute-mapping` that maps `assertion.sub`, `assertion.repository` and `assertion.repository_owner`, an `--attribute-condition` such as `assertion.repository_owner == 'ORG'` (never omit it), and IAM bindings to `principalSet://iam.googleapis.com/projects/NUMBER/locations/global/workloadIdentityPools/POOL/attribute.repository/ORG/REPO`.

GitLab to AWS: provider URL `https://gitlab.com`, subject `project_path:GROUP/PROJECT:ref_type:branch:ref:main`, plus `gitlab.com:project_id` (stable across renames).

## 4. Audit the trust policy before it merges

```bash
aws iam get-open-id-connect-provider --open-id-connect-provider-arn arn:aws:iam::ACCOUNT:oidc-provider/token.actions.githubusercontent.com > provider.json
python3 minioidc.py audit --trust-policy trust.json --provider provider.json      # exit 1 on HIGH or CRITICAL
```

| Finding | Meaning | Action |
|---|---|---|
| `missing-subject`, `wildcard-subject` | any repository on the issuer gets in | block; IAM rejects it on save, but older roles still have it |
| `wildcard-owner` | a look-alike organization gets in | block; end the owner with `/` |
| `negated-subject`, `unmapped-key-passes` | the condition restricts nothing | block; replace with `StringEquals` on `:sub` |
| `wildcard-repository`, `wildcard-context` | other repositories, branches, pull requests or environments | block for write roles; allow for read-only with a written reason |
| `pull-request-subject` | unreviewed code gets the role | read-only roles only |
| `missing-audience`, `provider-many-audiences` | tokens minted for another audience may be accepted | add `:aud` = `sts.amazonaws.com` |
| `unmapped-key-fails`, `literal-wildcard` | the role can never be assumed | fix before merge; the deploy will fail |

Also check every existing role that trusts the provider, not only the changed one:

```bash
aws iam list-roles --query "Roles[?contains(to_string(AssumeRolePolicyDocument), 'token.actions.githubusercontent.com')].RoleName" --output text
```

## 5. Wire the workflow

```yaml
permissions:
  contents: read
jobs:
  deploy:
    environment: prod
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: aws-actions/configure-aws-credentials@FULL_COMMIT_SHA # vX.Y.Z
        with:
          role-to-assume: arn:aws:iam::ACCOUNT:role/deploy-prod
          role-duration-seconds: 900
          aws-region: REGION
```

Grant `id-token: write` only to the jobs that federate. Pin every action by full commit SHA and let Dependabot or Renovate update the pins. Keep `role-duration-seconds` close to the job's real length.

## 6. Prove it from one job

1. Run the workflow once. `aws sts get-caller-identity` must print `arn:aws:sts::ACCOUNT:assumed-role/deploy-prod/SESSION`.
2. Run the same job from a feature branch or without the environment. It must fail with `AccessDenied`. A success means the subject is wider than designed.
3. Find the call in CloudTrail and read the subject it recorded:

```bash
aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRoleWithWebIdentity --max-results 5 \
  --query 'Events[].CloudTrailEvent' --output text
```

## 7. Cut over and delete the old keys

Remove the old secrets from the repository, organization and environments; deactivate the access keys (`aws iam update-access-key --status Inactive`); watch `get-access-key-last-used` and the pipeline for a week; then delete the keys and, if nothing else uses it, the IAM user. Rotating a key you are about to delete is wasted work.

## 8. Triage a failing exchange

| Error | Cause | Check | Fix |
|---|---|---|---|
| the action cannot load credentials, or `ACTIONS_ID_TOKEN_REQUEST_URL` is unset | the job lacks `id-token: write`, or it is a pull request from a fork | the `permissions` block of the job and workflow | grant it on that job |
| `InvalidIdentityToken: No OpenIDConnect provider found in your account for ...` | wrong account, or the provider URL differs from `iss` | `aws iam list-open-id-connect-providers` in the role's account | create the provider in that account |
| `InvalidIdentityToken: Incorrect token audience` | the step asked for another audience | decode the token's `aud` | `audience: sts.amazonaws.com`, or add the client ID to the provider |
| `InvalidIdentityToken` mentioning the verification key | the JWKS cannot be fetched, publishes private members, or the `kid` is unknown | fetch the discovery document and JWKS | fix the issuer; rotate any key whose private part was published |
| `ExpiredTokenException` | the token was fetched long before the call, or clocks differ | `iat` and `exp` in the token | request the token right before the call |
| `AccessDenied: Not authorized to perform sts:AssumeRoleWithWebIdentity` | the trust policy: almost always `sub` | decode the token and compare `sub` character by character with the condition (`minioidc assume` prints the failing condition) | change the condition or the job (environment, branch, template) |
| `ValidationError ... MaxSessionDuration` | `role-duration-seconds` above the role's maximum | `aws iam get-role --query Role.MaxSessionDuration` | lower the duration or raise the maximum |
| `AccessDenied` on a later API call | the role's permissions, not federation | the AccessDenied text and the role's policies | fix the permissions policy |

## 9. Report

State: every stored credential found, its owner and status (migrated, deactivated, deleted, kept with a reason and a date); each new role with its subject, audience, permissions policy and maximum session; the audit result for every role that trusts the provider; the proof (the allowed run's assumed-role ARN and the denied run's error); the workflows changed with their pinned SHAs; and anything left open. Do not approve a role whose trust policy has a CRITICAL or HIGH finding, an `id-token: write` granted at workflow level to jobs that do not federate, or a migration that leaves the old key active.
