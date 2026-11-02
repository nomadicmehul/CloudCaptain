# A Leaked Key Is a Pattern, Entropy and History

*A pushed key lives in every clone: find it by shape, entropy and history, revoke it first, then rewrite history and block the next one.*

## Metadata

| Field | Value |
|---|---|
| Type | Build |
| Languages | Python |
| Prerequisites | [A Workflow Is Code That Holds Your Secrets](https://cloudcaptain.io/learn/cicd/workflow-security) |
| Time | 75 minutes |
| Tools | gitleaks, git, git filter-repo, pre-commit, TruffleHog |

<Quiz stage="pre" />

## The Problem

Thursday, 14:05. A developer chasing a failing deploy pastes an AWS access key and its secret into `config/app.env`, commits "wip" and pushes the branch to the company's public SDK repository so a colleague can reproduce it. Eleven minutes later they notice, run `git rm config/app.env`, commit "remove config" and push again. The pull request diff is now clean, the review is approved, and the CI job called "secret scan" is green. At 17:40 the billing alarm fires: forty GPU instances in three Regions nobody uses. The key had been tried from an unknown network four minutes after the first push. Bots that watch public pushes do not read pull request diffs; they read every commit.

The green CI job ran `gitleaks dir .` on the checked-out head, so it saw the working tree after the deletion and nothing else. It used to scan history, but the first full run produced 312 findings, most of them `password = ...` lines in test fixtures and a documentation page full of example keys, and someone added `continue-on-error: true` "until we clean it up". The incident review runs a real history scan and finds three more live credentials: a Slack bot token pasted into a commit message two years ago ("temp token for the alert bot"), a deploy key in a file deleted in 2024, and a database password that is still in production. Then someone force-pushes a rewritten history and declares the problem solved, while the AWS key is still active: rewriting the repository on the server does nothing to the forks, the clones, the CI caches and the bot that already copied the key.

Each of those failures has a precise cause. A scanner that does not read history misses everything that was ever deleted. A scanner without precise suppression (allowlists for fixtures, inline ignores for intentional test values, a baseline of known findings) floods its users until they switch it off. And a response that starts with `git filter-repo` instead of revoking the credential at its issuer cleans the evidence and leaves the door open. You are going to build `minileaks`, a scanner for files, Git history and staged changes with provider rules, entropy, allowlists, baselines and a pre-commit hook, run gitleaks with the same rules on the same repository, and practise the response in the right order.

## The Concept

Think of a border check. An officer recognises a passport at a glance by its shape: cover, chip, machine-readable line. A handwritten note has no shape, so the officer reads the context ("this is my password") and judges whether the contents look made up. And luggage that went through yesterday is still in the building, on a shelf nobody looks at. Secret scanning is those three checks: shape, randomness, and history.

**Shape.** Providers design tokens to be found. AWS access key IDs are 20 characters starting with `AKIA` (long-term keys) or `ASIA` (temporary STS credentials), followed by 16 uppercase letters and digits. GitHub tokens start with `ghp_`, `gho_`, `ghu_`, `ghs_` or `ghr_`, or `github_pat_` for fine-grained tokens; the underscore was chosen because it is not a Base64 character, so random strings and SHAs do not produce it. Slack tokens start with `xoxb-` or `xoxp-`, PEM private keys sit between `-----BEGIN ... PRIVATE KEY-----` armour lines, and a JSON Web Token's header starts with `eyJ`, the Base64url encoding of `{"` followed by a letter. A rule is a regular expression for that shape plus a short list of **keywords**. Before any regex runs, the scanner lowercases the text once and checks which keywords occur (gitleaks does this for all keywords at once with an Aho-Corasick automaton); a rule whose keywords are absent never runs. On ordinary source files, most rules are skipped that way, which is why scanning a large history takes seconds.

**Randomness.** A password or an API key in a generic `secret = ...` assignment has no shape. What it has is randomness, measured by Shannon entropy: for the characters of the captured value with frequencies p, H = -Σ p·log2(p) bits per character. `aaaa` scores 0, `abab` scores 1, a 40-character random Base64 string scores about 4.7, and a string of n characters can never score more than log2(n), 5.3 for 40 characters. Entropy alone floods: UUIDs, hashes, minified JavaScript and Base64 images are random too. So a generic rule is three filters stacked: a keyword in the key name, a value above an entropy threshold, and an allowlist that drops identifiers (`password = getPassword`) and references (`${DB_PASSWORD}`). Every threshold trades false positives for false negatives: `hunter2hunter2` scores 2.8 and is not reported, although it is a real, bad password.

**History.** Git stores snapshots, and `git log -p` shows each commit as the lines it added and removed. A secret exists from the commit that added it, in every clone made since, whatever later commits do; `git rm` adds a commit without the file and leaves the old blob reachable from the old commit. So the unit of scanning is "the added lines of every commit reachable from any ref", not "the files in the checkout", and commit messages are text that can hold a pasted token too. gitleaks reads exactly `git log -p -U0 --full-history --all --diff-filter=tuxdb`; it does not scan commit messages, `minileaks` does.

**Precise suppression.** A gate that fails on every fixture is turned off within a week. Three levels keep it on. An **allowlist** drops paths (`testdata/`), values (the AWS documentation key `AKIAIOSFODNN7EXAMPLE`) or stopwords (`example`, `changeme`). An **inline ignore** (`gitleaks:allow` on the line) marks one intentional test value where reviewers see it in the diff. A **baseline** is a previous report: its findings stop failing the build, new findings still fail, and the old ones go on a rotation list instead of being forgotten.

**Response order.** A leaked credential is an incident at its issuer, not a Git problem. Revoke or rotate it first, read the issuer's logs for use since the commit date, then remove it from code, and only then decide whether to rewrite history. Rewriting changes every commit hash after the first rewritten commit, breaks open pull requests and does not reach other people's clones; GitHub's own guidance calls it often unnecessary once the credential is revoked, and necessary when the data cannot be rotated.

```mermaid
flowchart LR
  SRC["source: files, git log -p --all, git diff --staged"] --> FR["fragments: added lines with commit, file, line"]
  FR --> PA{"path or commit allowlisted?"}
  PA -->|"yes"| SKIP["skip the fragment"]
  PA -->|"no"| KW["keyword prefilter picks the rules"]
  KW --> RX["rule regex, capture the secret"]
  RX --> IN{"gitleaks:allow on the line?"}
  IN -->|"no"| EN{"entropy above the threshold?"}
  EN -->|"yes"| AL{"value allowlisted?"}
  AL -->|"no"| DD["drop generic duplicates"]
  DD --> BL{"in the baseline?"}
  BL -->|"no"| OUT["finding: commit, file, line; exit 1"]
  OUT --> ROT["revoke at the issuer, then rewrite history"]
```

## Build It

`code/main.py` is `minileaks`, standard library only, with six commands: `dir`, `git`, `staged`, `rules`, `entropy` and `install-hook`. Every Git call goes through an injectable runner, so the tests feed it captured `git log -p` text, plus one test that builds a real repository when `git` is installed. Nothing token-shaped is stored in this repository, because GitHub's secret scanning runs on it: the tests generate every token at run time with `secrets`, and the fixture `code/fixtures/git-log-p.txt` holds placeholders such as `@AWS_KEY_ID@` that the tests replace. The only literal key anywhere is AWS's documented example, `AKIAIOSFODNN7EXAMPLE`.

### Step 1: entropy in bits per character

```python
def shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts: Dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    length = len(text)
    return 0.0 - sum((n / length) * math.log2(n / length) for n in counts.values())
```

```bash
python3 code/main.py entropy aaaa abab abcd hunter2hunter2 'Zq8!vT3#Lm9%Wd2@xR5^'
# 0.000     4 chars  aaaa
# 1.000     4 chars  abab
# 2.000     4 chars  abcd
# 2.807    14 chars  hunt****
# 4.322    20 chars  Zq8!****
```

The last value is the ceiling for 20 characters: log2(20) = 4.32, reached only when no character repeats. That ceiling is why entropy thresholds only work on values of a known minimum length, and why every rule applies its threshold to the captured secret, never to the whole line.

### Step 2: a rule is a regex, keywords, a threshold and an allowlist

```python
    {
        "id": "aws-access-token",
        "description": "AWS access key ID (long-term AKIA, temporary ASIA)",
        "regex": r"\b((?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16})\b",
        "keywords": ["a3t", "akia", "asia", "abia", "acca"],
        "entropy": 3.0,
        "rotate": ROTATE_AWS,
        "allowlists": [{"description": "AWS documentation example key", "regexes": [r".+EXAMPLE$"]}],
    },
```

The twelve default rules (`python3 code/main.py rules`) cover AWS key IDs and secret keys, four GitHub token types, three Slack formats, private key blocks, JWTs and generic assignments. The IDs, most regexes and the thresholds follow gitleaks' default rules; the JWT and generic rules are simpler than gitleaks' versions, and gitleaks has no AWS secret key rule at all (its generic rule covers it), so `aws-secret-access-key` here requires an `aws` keyword in the key name and 40 characters above 4.0 bits. All patterns are written in the common subset of Python's `re` and RE2, the engine gitleaks uses: no lookarounds, no backreferences, inline flags only at the start or scoped as `(?i:...)`. Every pattern is compiled with `re.ASCII`, so `\w`, `\b` and `\s` mean what they mean in RE2. That is what lets `rules --format gitleaks` export the same rules as a gitleaks TOML file.

A rule that loads from a JSON config (`--config`) is checked when it is built: an unknown key, a regex that does not compile, a `secret_group` beyond the regex's groups or an unknown validator stop the scan with exit 2 and the rule's ID in the message.

### Step 3: the pipeline, in gitleaks' order

For each fragment (one file, or the added lines of one hunk), `detect()` checks the path and commit allowlists, lowercases the text once and runs only the rules whose keywords occur:

```python
    lowered = fragment.text.lower()
    findings: List[Finding] = []
    for rule in config.rules:
        if rule.keywords and not any(k in lowered for k in rule.keywords):
            stats.prefiltered += 1
            continue
        stats.rule_runs += 1
        findings.extend(detect_rule(fragment, rule, config, stats, inline_ignore))
    return drop_generic_duplicates(findings, stats)
```

Inside `detect_rule()` each match goes through the same filters in the same order as gitleaks' `detectRule`, and every filter counts what it removed, so a report says why it is quiet:

```python
        if inline_ignore and any(marker in line_text for marker in INLINE_IGNORE):
            stats.suppressed["inline"] += 1
            continue
        secret = pick_secret(match, rule, whole)
        entropy = shannon_entropy(secret)
        if rule.entropy and entropy <= rule.entropy:
            stats.suppressed["entropy"] += 1
            continue
        if rule.validate and not VALIDATORS[rule.validate](secret):
            stats.suppressed["invalid"] += 1
            continue
        if any(a.value_allowed(secret, whole, line_text) for a in list(config.allowlists) + list(rule.allowlists)):
            stats.suppressed["allowlist"] += 1
            continue
```

`pick_secret()` returns the rule's `secret_group`, or the first non-empty group, or the whole match: for `AWS_ACCESS_KEY_ID=AKIA...` the secret is the key, not the line. The one validator, `valid_jwt()`, decodes the header and requires a JSON object with an `alg` field; it is a structural check, the offline cousin of the live verification TruffleHog does. Finally, `drop_generic_duplicates()` removes a generic finding when a provider rule found a secret containing it on the same line of the same commit, exactly as gitleaks' `filter()` does: the line `aws_secret_access_key = ...` matches both rules, and the specific one wins.

### Step 4: line numbers come from offsets

A fragment knows the line it starts on; a match knows its character offsets. Counting newlines before the offset gives the line:

```python
def locate(text: str, start: int, end: int) -> Tuple[int, int, int, int, int, int]:
    line_offset = text.count("\n", 0, start)
    line_start = text.rfind("\n", 0, start) + 1
    last = max(start, end - 1)
    end_offset = text.count("\n", 0, last)
    end_line_start = text.rfind("\n", 0, last) + 1
    line_end = text.find("\n", last)
    if line_end == -1:
        line_end = len(text)
    return line_offset, start - line_start + 1, end_offset, end - end_line_start, line_start, line_end
```

A private key block spans several lines, so it gets a start and an end line, and `line_text` (from the start of the first line to the end of the last) is what the inline-ignore check reads. Columns are 1-based like an editor's; gitleaks' columns are one higher on every line after the first of a fragment, so the smoke compares lines, not columns.

### Step 5: history is `git log -p` parsed into added lines

```python
def log_args(repo: str, log_opts: str = "") -> List[str]:
    selection = log_opts.split() if log_opts else GIT_LOG_FLAGS
    return git_base(repo) + ["log", "-p", "-U0"] + selection + GIT_FORMAT_FLAGS


def message_args(repo: str, log_opts: str = "") -> List[str]:
    selection = log_opts.split() if log_opts else ["--all"]
    return git_base(repo) + ["log", "-z", "--date=iso-strict", MESSAGE_FORMAT] + selection
```

`GIT_LOG_FLAGS` is gitleaks' selection, `--full-history --all --diff-filter=tuxdb`: every ref, every commit, and the lowercase filter letters exclude deleted files (`d`) along with type changes, unmerged, unknown and broken pairs. A deleted file adds no lines, and the commit that added it is still in the log. `GIT_FORMAT_FLAGS` pins what user configuration could change: no colour, no external diff driver, no decorations, ISO dates and the `medium` format, whose message lines are indented by four spaces and so can never be mistaken for a diff line. `-U0` drops context lines, and `core.quotePath=false` keeps non-ASCII names readable. With the default setting, or for names that contain a quote, a backslash or a control character, git prints the path C-quoted (`"b/caf\303\251.py"`, as in the fixture), and names with a space carry a trailing tab; `unquote_path()` handles both.

`parse_patch()` is a small state machine: a `commit <sha>` line reads the header (author, email, date converted to UTC), `+++ b/path` names the file, and each `@@ -a,b +c,d @@` header says how many old and new lines follow. `added_runs()` consumes exactly that many, collects the `+` lines with their line numbers in the new file and skips `\ No newline at end of file`:

```python
        if text.startswith("+"):
            if runs and runs[-1][0] + len(runs[-1][1]) == line_no:
                runs[-1][1].append(text[1:])
            else:
                runs.append((line_no, [text[1:]]))
            seen_new += 1
            line_no += 1
        elif text.startswith("-"):
            seen_old += 1
        elif text.startswith(" "):
            seen_old += 1
            seen_new += 1
            line_no += 1
```

Counting lines instead of looking for the next `diff --git` is what makes the parser safe on files whose content looks like a diff. Commit messages come from a second command, because `--diff-filter` also hides commits with no matching file change (merges, empty commits, a commit that only deletes): `-z` separates commits with NUL bytes, `%x1f` separates the fields, and each message becomes a fragment with `where: "message"` and no file.

### Step 6: suppress precisely

An allowlist matches the secret by default (`regex_target` can be `match` or `line`), and its stopwords match anywhere in the lowercased secret. The default global allowlist skips `.git`, lock files and binary assets and carries stopwords such as `example` and `changeme`; the generic rule adds its own allowlist for identifiers, attribute paths such as `settings.db_password`, `$VARIABLE` references and `@PLACEHOLDER@` templates (the form this lesson's fixture uses). Paths are regexes searched in the path, so `(?:^|/)testdata/` skips a whole directory in `dir` mode and in history.

The baseline is a previous report. A minileaks report never contains raw secrets, so the baseline keys on the SHA-256 of the secret:

```python
    def contains(self, finding: Finding) -> bool:
        key = (finding.rule, finding.file, finding.commit, finding.secret_sha256)
        return key in self.keys or finding.fingerprint in self.fingerprints
```

Leaving the line number out of the key means a known finding in the working tree stays known when someone adds lines above it; including the commit means the same old secret committed again in a new commit is new, which is what you want to hear about. `baseline_entry()` also reads gitleaks' JSON reports: their `Secret` field is hashed, and a redacted gitleaks report (`REDACTED` or a value ending in `...`) falls back to gitleaks' fingerprint, `commit:file:rule:line`. gitleaks' own baseline compares every field of a finding, including the line, the columns, the author and the entropy, so in a `dir` scan a moved line comes back as new.

### Step 7: pre-commit mode only reads what is staged

```python
def staged_args(repo: str) -> List[str]:
    return git_base(repo) + ["diff", "-U0", "--no-color", "--no-ext-diff", "--staged", "."]
```

`git diff --staged` is the commit about to be made, in the same patch format, so `parse_patch()` reads it unchanged; findings have no commit and fingerprints of the form `file:rule:line`. `install-hook` writes `.git/hooks/pre-commit` (wherever `git rev-parse --git-path hooks` points, so `core.hooksPath` is honoured) as a two-line shell script that runs `minileaks staged` with the current Python interpreter. It refuses to overwrite a hook it did not write unless you pass `--force`. A local hook is a seatbelt, not a gate: `git commit --no-verify` skips it, which is why the same scan also runs in CI and on the server.

### Step 8: reports a machine can act on

`--json` reports carry the rule, file, line and column, commit, author, date, the fingerprint, the SHA-256 of the secret and its first four characters followed by `****`, never the secret itself; `--sarif` writes SARIF 2.1.0 with a physical location per file finding (a commit message finding gets a logical location naming the commit). Every report ends with the rotation steps per rule and the earliest commit date, which is where the issuer's log search starts. `--replace-text FILE` writes the expressions `git filter-repo` needs, with mode 0600 because, unlike the report, it has to contain the secrets:

```python
        if "\n" in finding.secret:
            expression = "regex:" + "\\n".join(re.escape(part) for part in finding.secret.split("\n"))
        else:
            expression = "literal:" + finding.secret
        line = f"{expression}==>{replacement}"
```

filter-repo reads one expression per line, so a multi-line private key block becomes an escaped regex with `\n` between its lines. Exit codes: 0 when nothing is reported, 1 when something is, 2 for a bad config or baseline, a path that does not exist, or a `git` command that fails.

### Step 9: run it

Build a repository with a key that was committed and then deleted. The tokens are generated with the right shape and are not real credentials:

```bash
L=$PWD
python3 code/main.py rules
R=$(mktemp -d) && cd "$R" && git init -q -b main
python3 - <<'EOF'
import secrets, string
upper, alnum = string.ascii_uppercase + "234567", string.ascii_letters + string.digits
key = "AKIA" + "".join(secrets.choice(upper) for _ in range(15)) + "7"
secret = "".join(secrets.choice(alnum + "/+") for _ in range(39)) + "9"
open("app.env", "w").write(f"REGION=eu-central-1\nAWS_ACCESS_KEY_ID={key}\naws_secret_access_key = {secret}\nDOCS_KEY=AKIAIOSFODNN7EXAMPLE\n")
EOF
git add app.env && git commit -qm "add config" && git rm -q app.env && git commit -qm "remove config"
python3 $L/code/main.py dir .; echo "exit $?"
# minileaks dir .: 0 finding(s) in 0 fragment(s), 0 file(s), 0 commit(s)
# suppressed: ...; 1 path(s) allowlisted                   (the .git directory)
# exit 0
python3 $L/code/main.py git .; echo "exit $?"
#   aws-access-token         app.env:2     commit 9ed4277  AKIA****   entropy 3.68
#   aws-secret-access-key    app.env:3     commit 9ed4277  41km****   entropy 4.48
# suppressed: entropy 0, invalid 0, inline 0, allowlist 1, duplicate 2, baseline 0; keyword prefilter skipped 33 of 36 rule runs
# rotate first, then rewrite history: ...
# exit 1
```

The working tree is clean and the history is not. `allowlist 1` is the documentation key, and `duplicate 2` are the generic rule's matches on the two AWS lines, shadowed by the specific rules. Now accept what is known and keep failing on what is new:

```bash
python3 $L/code/main.py git . --json --report-path baseline.json     # 2 finding(s); json report written to baseline.json
python3 $L/code/main.py git . --baseline baseline.json; echo "exit $?"   # 0 findings, baseline 2, exit 0
python3 $L/code/main.py git . --sarif > leaks.sarif
python3 $L/code/main.py git . --replace-text ../expressions.txt > /dev/null; ls -l ../expressions.txt   # -rw-------
python3 $L/code/main.py install-hook . && cp $L/code/fixtures/git-log-p.txt notes.txt && git add notes.txt && git commit -qm "notes"; echo "exit $?"   # the fixture has only placeholders: exit 0
```

<RunTests />

## Use It

gitleaks does the same job in Go with 222 default rules (in release 8.30.1). Install a pinned release and check it against the checksum file published with it; `code/smoke.sh` additionally pins the archive's SHA-256, read from the release:

```bash
GITLEAKS_VERSION=8.30.1
BASE=https://github.com/gitleaks/gitleaks/releases/download/v$GITLEAKS_VERSION
ASSET=gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz
curl -fsSLO $BASE/$ASSET
curl -fsSLO $BASE/gitleaks_${GITLEAKS_VERSION}_checksums.txt
grep " $ASSET\$" gitleaks_${GITLEAKS_VERSION}_checksums.txt | sha256sum -c -
tar -xzf $ASSET gitleaks && sudo install -m 0755 gitleaks /usr/local/bin/gitleaks
gitleaks version                                   # 8.30.1
```

**The scans.** `gitleaks git` reads history with the command from Step 5, `gitleaks dir` reads files, and the old `gitleaks detect` and `gitleaks protect` commands, deprecated since 8.19.0, still run as hidden commands you will meet in older docs:

```bash
gitleaks git . --report-format json --report-path gl.json    # exit 1 when anything is found
gitleaks git . --log-opts="origin/main..HEAD"                 # only the commits of a pull request
gitleaks dir . -f sarif -r gl.sarif                           # the working tree, as SARIF
gitleaks git . --baseline-path gl.json -f json -r new.json    # only findings missing from the earlier report
gitleaks git --pre-commit --staged --redact --verbose         # what the pre-commit hook runs
```

**The same rules in both.** `minileaks rules --format gitleaks` writes the active rules and allowlists as gitleaks TOML, so the two scanners can be compared on equal terms; the smoke does exactly that. In your own repository you usually extend gitleaks' defaults instead:

```toml
[extend]
useDefault = true

[[rules]]
id = "internal-token"
description = "Acme internal service token"
regex = '''itk_[a-z0-9]{24}'''
keywords = ["itk_"]
entropy = 3.5

[[allowlists]]
description = "test fixtures"
paths = ['''(?:^|/)testdata/''']
```

Save it as `.gitleaks.toml` at the repository root (gitleaks finds it there) or pass `-c`. A finding you accept goes either inline (`# gitleaks:allow` on that line, visible in review) or into `.gitleaksignore` as its fingerprint, `commit:file:rule:line`.

**The pre-commit framework.** The gitleaks repository publishes a hook definition, so the hook is three lines of configuration instead of a script:

```yaml
repos:
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.30.1
    hooks:
      - id: gitleaks
```

`pre-commit install` writes `.git/hooks/pre-commit`; the hook runs `gitleaks git --pre-commit --redact --staged --verbose`. Like `minileaks install-hook`, it is skipped by `git commit --no-verify`.

**The gate in CI.** Scan the commits a pull request adds, with the full history fetched and the SHAs passed through the environment, never pasted into the script (the previous lesson's rule):

```yaml
jobs:
  secrets:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@<full-commit-sha>
        with:
          fetch-depth: 0
      - name: gitleaks
        env:
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
          HEAD_SHA: ${{ github.event.pull_request.head.sha }}
        run: |
          ./scripts/install-gitleaks.sh 8.30.1        # the pinned, checksum-verified install above
          gitleaks git . --log-opts="$BASE_SHA..$HEAD_SHA" --redact -f sarif -r gitleaks.sarif
```

**Verification.** TruffleHog's detectors go one step further than a validator: for formats with an API they try the credential (for an AWS key, the equivalent of `aws sts get-caller-identity`) and label each result verified, unverified or unknown:

```bash
trufflehog git file://. --results=verified,unknown --fail
```

A verified finding is an incident now. An unverified one may already be revoked or may be a false positive, so it still goes through triage. Verification sends the credential to its issuer from your machine, which is fine for your own leaks and appears in the issuer's logs as a use of the key.

**On the server.** GitHub secret scanning reads the entire Git history on all branches, and for tokens from partner providers it notifies the provider, who may revoke them (AWS attaches a quarantine policy to a leaked key's user). Push protection checks pushes before they land: the personal "push protection for users" is enabled by default and stops your pushes of supported secrets to public repositories, and push protection for a repository or organisation needs GitHub Secret Protection and is off until an administrator enables it. A blocked push names the secret type, commit and path; bypassing it needs a reason and creates an alert. Alerts are an API:

```bash
gh api repos/OWNER/REPO/secret-scanning/alerts --jq '.[] | [.number, .secret_type, .state, .validity] | @tsv'
```

**Rewriting history.** After the credential is revoked, `git filter-repo` (the tool Git's own documentation recommends instead of `filter-branch`) rewrites every commit that contains it. GitHub's procedure, which needs filter-repo 2.47 or later for `--sensitive-data-removal`:

```bash
pip install git-filter-repo==2.47.0
git clone https://github.com/OWNER/REPO && cd REPO
python3 $L/code/main.py git . --replace-text ../expressions.txt > /dev/null
git filter-repo --sensitive-data-removal --replace-text ../expressions.txt --replace-message ../expressions.txt
git filter-repo --sensitive-data-removal --invert-paths --path deploy/id_ed25519     # or drop a whole file from every commit
python3 $L/code/main.py git .; echo "exit $?"                                        # 0: nothing left in this clone
grep -c '^refs/pull/.*/head$' .git/filter-repo/changed-refs                          # pull requests whose diffs will break
git push --force --mirror origin                                                     # refs/pull/* fail: GitHub keeps them read-only
```

`--sensitive-data-removal` first fetches every ref from `origin`, so no branch keeps the old history. Old objects stay on the server until it is garbage-collected, and cached views and pull request refs can only be removed by GitHub Support, which helps only when rotating the credential cannot mitigate the risk. Every collaborator must re-clone or rebase; anyone who merges an old branch brings the secret back, and your baseline then reports it as new, because its commit is new.

| What you built | Real tool | Note |
|---|---|---|
| `shannon_entropy()`, `entropy` per rule | gitleaks `entropy = 3.5` in a rule | both skip a match whose secret is at or below the threshold |
| `DEFAULT_RULES`, `rules --format gitleaks` | gitleaks' default config, `[extend] useDefault = true` | 12 rules here, 222 in gitleaks 8.30.1 |
| keyword prefilter with `in` | Aho-Corasick over all keywords | same result, one pass instead of one per keyword |
| `parse_patch()` over `git log -p -U0 --full-history --all --diff-filter=tuxdb` | `gitleaks git` | same command; gitleaks can also open archives in history (`--max-archive-depth`) and decode Base64 or hex segments |
| `parse_messages()` | none in gitleaks | GitHub secret scanning also covers text such as issues and pull requests |
| `staged`, `install-hook` | `gitleaks git --pre-commit --staged`, the pre-commit framework | `git commit --no-verify` skips both |
| global and rule allowlists | `[[allowlists]]`, `[[rules.allowlists]]` | gitleaks adds `condition = "AND"` and `targetRules` |
| `gitleaks:allow` and `minileaks:allow` | `gitleaks:allow`, `--ignore-gitleaks-allow` | the marker is checked on the finding's whole line |
| `Baseline` keyed on the secret's SHA-256 | `--baseline-path`, `.gitleaksignore` | gitleaks compares every field, so a moved line returns as new |
| `valid_jwt()` | TruffleHog verification, GitHub validity checks | structure offline versus a live call to the issuer |
| `report_sarif()` | `-f sarif`, code scanning upload | gitleaks puts the raw secret in its snippet unless you pass `--redact` |
| `--replace-text` expressions | `git filter-repo --replace-text`, `--replace-message` | rewriting revokes nothing |

<Quiz stage="check" />

## Ship It

Two artifacts in `outputs/`:

- **`skill-leaked-secret-response.md`**: a runbook for the moment a scanner, a push protection block or a provider's email says a credential leaked. It contains first (revoke or rotate at the issuer, with the commands per provider), scopes the exposure (every commit, branch, fork and message that holds it, and the earliest date), reads the issuer's logs, cleans the code, decides with a table whether to rewrite history and how, then closes the gap that let it in (hook, CI gate, push protection, baseline) and ends with a fixed report.
- **`prompt-triage-secret-finding.md`**: paste one or more findings from `minileaks --json`, a gitleaks report or a GitHub secret scanning alert, with the redacted line and the path, and get a verdict per finding (live credential, test fixture, placeholder, already revoked, needs verification), the severity, the exact rotation steps for that provider, whether history must be rewritten, and the allowlist, inline ignore or baseline entry if it is not a real secret.

<ShipIt />

## Exercises

### Easy

Add a `.minileaksignore` file like gitleaks' `.gitleaksignore`: one fingerprint per line (`commit:file:rule:line` or `file:rule:line`), blank lines and `#` comments ignored, read from the scan root or from `--ignore-file PATH`, with suppressed findings counted under a new `ignored` reason. Write the tests first: a fingerprint from a `--json` report hides exactly that finding, a fingerprint with another line number hides nothing, and a malformed line exits 2 with its line number.

### Medium

Add `condition: "AND"` and `target_rules` to allowlists, as gitleaks has: an AND allowlist applies only when every check it defines matches (for example, allow `generic-password` values only under `docs/` and only when the value contains `example`), and `target_rules` attaches a global allowlist to the named rules only. Tests first, including a path that matches but a value that does not, an unknown rule ID in `target_rules` (exit 2), and the TOML export, which must write `condition = "AND"` and `targetRules` so that gitleaks agrees on the smoke repository.

### Hard

Add `--verify` with an injectable HTTP client. For `github-pat` and `github-fine-grained-pat` findings, call `GET https://api.github.com/user` with `Authorization: Bearer <token>` and label the finding `verified` on 200, `invalid` on 401 and `unknown` on anything else or a timeout; print verified findings first and exit 3 when any is verified. The tests use a fake client and never touch the network: assert the request's method, URL and header, the three labels, that a 429 is `unknown` rather than `invalid`, and that the token never appears in the JSON report or in the client's log line.

<Quiz stage="post" />

## Key Terms

| Term | Common misconception | Precise definition |
|---|---|---|
| Detection rule | "A regex is enough to find secrets." | A regex for the token's shape plus keywords that decide whether it runs, an optional entropy threshold on the captured secret, validators and allowlists. Without the last three, generic rules flood the report. |
| Keyword prefilter | "Keywords are what makes something a secret." | A cheap case-insensitive substring test that selects which rules run on a fragment; a match still has to pass the regex and every filter after it. |
| Shannon entropy | "High entropy means it is a secret." | Bits per character of the value's character distribution, at most log2 of its length. Hashes, UUIDs and Base64 data score high too, so entropy is a filter on top of context, never a detector on its own. |
| Allowlist | "Allowlisting a path makes the secrets in it safe." | An instruction not to report matching paths, commits, values or stopwords. Anything under an allowlisted path must be fake, because a real key there is still leaked, just unreported. |
| Baseline | "A baseline means the old secrets were accepted as safe." | A previous report whose findings no longer fail the scan, so the gate can be switched on today. Every baselined real credential still needs rotating. |
| Inline ignore | "`gitleaks:allow` hides a secret from attackers." | A marker on the line that tells the scanner not to report a match there. It is for intentional fake values and is visible in review; the value is still in the repository. |
| History rewrite | "Deleting the file or force-pushing removes the secret." | Creating new commits without the value (`git filter-repo`) and replacing every ref. Clones, forks, caches, pull request refs and anyone who already copied the value keep it, which is why revocation comes first. |
| Push protection | "It replaces local and CI scanning." | A server-side check of each push for supported secret formats that can block it; it can be bypassed with a reason and only knows the formats its provider supports. |
| Verification | "An unverified finding is a false positive." | Calling the issuer with the credential to see whether it is live. Unverified can mean revoked, expired, an issuer without a test endpoint or a network error, so it still needs triage. |

## Further Reading

- [DevSecOps Fundamentals](https://cloudcaptain.io/docs/tools/devsecops/fundamentals): where secret scanning sits next to SAST, dependency and IaC scanning in a pipeline, and the secrets-management practices that keep keys out of code.
- [DevSecOps Cheat Sheet](https://cloudcaptain.io/docs/tools/devsecops/cheatsheet): gitleaks and TruffleHog commands next to the other scanners of this phase.
- [Git Advanced](https://cloudcaptain.io/docs/tools/git/advanced): hooks, refs and history, the mechanics this lesson's pre-commit mode and history rewrite rely on.
- [gitleaks](https://github.com/gitleaks/gitleaks): the configuration format, allowlists, baselines, `.gitleaksignore` and the default rules this lesson's rules follow.
- [Behind GitHub's new authentication token formats](https://github.blog/engineering/platform-security/behind-githubs-new-authentication-token-formats/): why tokens carry prefixes, an underscore and a checksum, from the people who designed them.
- [AWS IAM identifiers](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_identifiers.html): the unique ID prefixes (`AKIA`, `ASIA` and the rest) that make AWS keys recognisable.
- [About secret scanning](https://docs.github.com/en/code-security/secret-scanning/introduction/about-secret-scanning) and [About push protection](https://docs.github.com/en/code-security/secret-scanning/introduction/about-push-protection): what GitHub scans, partner notifications, and which push protection is on by default.
- [Removing sensitive data from a repository](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository): GitHub's full procedure with `git filter-repo --sensitive-data-removal`, and why rotating first often makes it unnecessary.
- [git-filter-repo manual](https://github.com/newren/git-filter-repo/blob/main/Documentation/git-filter-repo.txt): `--replace-text`, `--replace-message`, `--invert-paths` and the fresh-clone safety checks.
- [git log](https://git-scm.com/docs/git-log): `--all`, `--full-history`, `--diff-filter`, `-z` and the pretty formats the history parser depends on.
- [SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html): the report format code scanning platforms ingest.
