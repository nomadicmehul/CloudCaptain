---
name: leaked-secret-response
description: Respond to a leaked credential found by a scanner, push protection or a provider's notice; revoke it at the issuer first, scope every commit, branch and message that holds it, read the issuer's logs, clean code and (only when needed) history, close the gap, and report
version: 1.0.0
phase: security
lesson: security/secret-scanning
tags: [secrets, secret-scanning, gitleaks, incident-response, rotation, git, filter-repo, push-protection]
---

# Leaked secret response

Use this when `minileaks`, gitleaks, TruffleHog or GitHub secret scanning reports a credential, when push protection blocks a push, or when a provider (AWS, GitHub, Slack) tells you a key of yours was seen in public. The order matters: contain at the issuer, then scope, then investigate, then clean. Never paste the secret itself into a ticket, chat or prompt; refer to it by rule, file, commit, fingerprint and SHA-256. `minileaks.py` below is the lesson's `code/main.py`; gitleaks commands work the same way.

## 1. Identify it without spreading it

```bash
python3 minileaks.py git . --json > leaks.json                     # redacted: first 4 characters, sha256, fingerprint
gitleaks git . --redact -f json -r leaks-gl.json                   # gitleaks' view; --redact keeps the secret out of the report
gh api repos/OWNER/REPO/secret-scanning/alerts --jq '.[] | select(.state=="open") | [.number, .secret_type, .validity, .html_url] | @tsv'
python3 -c 'import json; [print(f["rule"], f["where"], f["file"], f["line"], f["commit"][:7], f["date"], f["secret"]) for f in json.load(open("leaks.json"))["findings"]]'
```

For each finding write down: the rule (which tells you the issuer), where it is (file and line, or commit message), the commit and its date, and whether the same SHA-256 appears more than once (one credential, several places). For an AWS key ID, find the owning account from anywhere with `aws sts get-access-key-info --access-key-id AKIA...`, and its user from inside that account with `aws iam get-access-key-last-used --access-key-id AKIA...`.

## 2. Contain at the issuer, now

| Rule | Contain | Confirm it is dead |
|---|---|---|
| `aws-access-token`, `aws-secret-access-key` (AKIA) | `aws iam update-access-key --user-name USER --access-key-id ID --status Inactive`; create and deploy a replacement; then `aws iam delete-access-key` | `aws iam list-access-keys --user-name USER` shows it Inactive or gone |
| `aws-access-token` (ASIA, temporary) | the session lasts until it expires: attach a deny-all policy to the role with a `DateLessThan` condition on `aws:TokenIssueTime` set to now (what the console's "Revoke active sessions" does), then find what leaked the session | CloudTrail shows `AccessDenied` for calls with that key |
| `github-pat`, `github-fine-grained-pat` | delete the token in the owner's Settings, Developer settings; organisation owners can revoke fine-grained tokens that access the organisation | `curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" https://api.github.com/user` prints 401 |
| `github-oauth`, `github-app-token` | revoke the app's authorisation for the user, or rotate the app's client secret or private key so new tokens cannot be minted | the same `/user` call prints 401 |
| `slack-bot-token`, `slack-user-token` | call `auth.revoke` with the token, or reinstall the app to rotate its tokens | `auth.test` with the token returns `ok: false` (`token_revoked` or `invalid_auth`) |
| `slack-webhook-url` | remove the webhook in the app's Incoming Webhooks settings and create a new one | a POST to the old URL fails |
| `private-key` | generate a new pair; remove the old public key everywhere it is trusted (`authorized_keys`, `gh repo deploy-key list` and `delete`, certificates, which must also be revoked) | `ssh -i old_key` is refused |
| `jwt` | revoke the session or refresh token behind it; rotate the signing key if tokens never expire or the key may have leaked | the API rejects the token |
| `generic-password` | change it at the system that checks it (for a database role, `ALTER ROLE ... PASSWORD`), then roll every consumer | a login with the old value fails |

Do this before anything else, even for a private repository: you do not know every clone, CI cache and laptop it reached. If you cannot revoke it yourself, page whoever can, and say "credential exposed, revoke now", not "please review".

## 3. Scope the exposure

```bash
git branch -a --contains COMMIT; git tag --contains COMMIT          # which refs carry the commit
git log --all --format='%H %aI %an %s' -n 1 COMMIT                   # who and when
gh api repos/OWNER/REPO/forks --paginate --jq '.[].full_name'        # forks have their own copy of the history
gh api repos/OWNER/REPO/activity --jq '.[] | select(.after=="COMMIT") | .timestamp'   # when it was pushed (public exposure starts here)
```

Record the earliest exposure time: the push time for a hosted repository, the commit date otherwise (`remediation.since` in the minileaks report). If the repository is or ever was public, assume the credential was copied within minutes of that time.

## 4. Look for use since then

| Issuer | Where to look |
|---|---|
| AWS | `aws cloudtrail lookup-events --lookup-attributes AttributeKey=AccessKeyId,AttributeValue=ID --start-time SINCE --region R` in every Region in use (management events, last 90 days); billing and Cost Explorer for new Regions or instance types |
| GitHub | the organisation audit log (UI, or the audit log API on GitHub Enterprise Cloud) filtered by the token's user; repository clones, new deploy keys, new webhooks, new workflow runs |
| Slack | the workspace access logs and the app's activity |
| Your own systems | authentication logs for the account behind the password or key |

Any use you cannot explain turns this into a security incident: hand over the timeline from steps 1 to 4 and keep going with step 5 in parallel.

## 5. Clean the code

Replace the value with a reference to a secret store, an environment variable injected at run time or a mounted file; add local files such as `.env` to `.gitignore`; commit. Scan the new commit: `python3 minileaks.py git . --log-opts "HEAD~1..HEAD"` must exit 0.

## 6. Decide about history

| Situation | Decision |
|---|---|
| Credential revoked, repository private, no policy requiring removal | Leave history. Add the finding to the baseline with a note naming the revocation ticket. |
| Credential revoked, repository public | Optional. The value is worthless, and forks and clones keep it anyway; rewrite only if policy requires it. |
| The value cannot be revoked (personal data, customer data, someone else's key, a signing key already distributed) | Rewrite now, and ask the host to purge cached views and pull request refs. |
| A file that should never have been tracked (a key file, a database dump) | Rewrite with `--invert-paths --path FILE`. |

To rewrite, on a fresh clone (git filter-repo 2.47 or later):

```bash
python3 minileaks.py git . --replace-text ../expressions.txt > /dev/null     # mode 0600; contains the secrets; delete it afterwards
git filter-repo --sensitive-data-removal --replace-text ../expressions.txt --replace-message ../expressions.txt
python3 minileaks.py git .; echo "exit $?"                                     # 0 before you push anything
grep -c '^refs/pull/.*/head$' .git/filter-repo/changed-refs                    # pull requests whose diffs will break
git push --force --mirror origin                                               # refs/pull/* are read-only on GitHub and will fail
shred -u ../expressions.txt 2>/dev/null || rm -f ../expressions.txt
```

Then tell every collaborator to re-clone (or rebase, never merge, their branches), and contact the host's support for cached views and pull request refs if step 6 said the data cannot be revoked.

## 7. Close the gap

- Install the hook: `python3 minileaks.py install-hook .` or the pre-commit framework with the `gitleaks` hook.
- Make the CI gate fail the build on new findings in the pushed range: `gitleaks git . --log-opts="$BASE_SHA..$HEAD_SHA" --redact`, with `fetch-depth: 0`.
- Turn on push protection for the repository or organisation; keep "push protection for users" on for your own account.
- If the format was not detected, add a rule (regex, keywords, entropy) and a test that generates a fake token of that shape.
- Only now add remaining accepted findings to the baseline; a baseline entry without a revocation reference is a finding hidden, not handled.

## 8. Verify

The confirm column of step 2 shows every credential dead; a fresh scan of every branch reports nothing new; the issuer's logs show no use after revocation; the replacement credential works where it is deployed.

## Report

State, in this order: what leaked (rule, issuer, redacted value, SHA-256 prefix) and where (repository, refs, files and lines, commit messages); the exposure window from first push to revocation, with times in UTC; the revocation and the evidence it worked; use found in the issuer's logs, or the exact searches that found none; code fixed (commit); the history decision from the table and, if rewritten, the refs and pull requests affected and the support request; the control added so it cannot recur; and open items with an owner and a date. Never include the secret, and never write "no impact" without the log searches that support it.
