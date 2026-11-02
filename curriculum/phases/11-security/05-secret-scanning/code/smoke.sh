#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

LESSON="$PWD"
GITLEAKS_VERSION="8.30.1"
GITLEAKS_SHA256_LINUX_X64="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
GITLEAKS_SHA256_LINUX_ARM64="e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"
FILTER_REPO_VERSION="2.47.0"
FILTER_REPO_WHEEL_SHA256="2cd04929b9024e83e65db571cbe36aec65ead0cb5f9ec5abe42158654af5ad83"
LOCAL_ONLY="${CC_SMOKE_LOCAL_ONLY:-}"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
mkdir -p "$WORK/bin" "$WORK/dl/x"
export PATH="$WORK/bin:$PATH"
export GIT_CONFIG_GLOBAL="$WORK/gitconfig" GIT_CONFIG_NOSYSTEM=1
git config --global user.name "CC Smoke"
git config --global user.email "smoke@example.test"
git config --global init.defaultBranch main
git config --global commit.gpgsign false

REPO="$WORK/repo"
CFG="$WORK/minileaks.json"
TOML="$WORK/gitleaks.toml"
STATE="$WORK/state.json"
COMMITS="$WORK/commits.txt"
: > "$COMMITS"

m() { python3 "$LESSON/code/main.py" "$@"; }
sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }
run_expect() {
  local want="$1" out="$2" got=0
  shift 2
  "$@" > "$out" 2> "$out.err" || got=$?
  [[ "$got" == "$want" ]] || fail "exit $got (want $want) from: $*
$(cat "$out" "$out.err")"
}
commit() {
  local stage="$1" when="$2"
  shift 2
  GIT_AUTHOR_DATE="$when" GIT_COMMITTER_DATE="$when" git -C "$REPO" commit -q "$@"
  echo "$stage $(git -C "$REPO" rev-parse HEAD)" >> "$COMMITS"
}
plant() { python3 "$WORK/plant.py" "$REPO" "$1" "$STATE"; }
in_repo() { (cd "$REPO" && "$@"); }
check() { python3 "$WORK/check.py" "$STATE" "$COMMITS" "$@"; }
have_gitleaks() { [[ -z "$LOCAL_ONLY" ]]; }

cat > "$WORK/plant.py" <<'PY'
import base64, hashlib, hmac, json, os, secrets, string, sys

repo, stage, state_path = sys.argv[1:4]
UPPER32 = string.ascii_uppercase + "234567"
ALNUM = string.ascii_letters + string.digits
state = json.load(open(state_path)) if os.path.exists(state_path) else {"expected": []}


def rand(alphabet, n):
    return "".join(secrets.choice(alphabet) for _ in range(n))


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def write(rel, lines):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def expect(rel, rule, line, where="file"):
    state["expected"].append({"stage": stage, "file": rel, "rule": rule, "line": line, "where": where})


def aws_key():
    return "AKIA" + rand(UPPER32, 15) + secrets.choice("234567")


def webhook():
    return "https://hooks.slack" + ".com/services/T" + rand(UPPER32, 8) + "/B" + rand(UPPER32, 8) + "/" + rand(ALNUM, 23) + secrets.choice(string.digits)


if stage == "c1":
    secret = rand(ALNUM + "/+", 38) + secrets.choice(string.digits) + "/"
    write("config/app.env", ["REGION=eu-central-1", "AWS_ACCESS_KEY_ID=" + aws_key(), 'aws_secret_access_key = "' + secret + '"', "AWS_DOCS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"])
    write("README.md", ["# billing-service", "", "Configuration lives in config/app.env."])
    expect("config/app.env", "aws-access-token", 2)
    expect("config/app.env", "aws-secret-access-key", 3)
elif stage == "c2":
    body = [base64.b64encode(os.urandom(48)).decode() for _ in range(4)]
    write("deploy/id_ed25519", ["-----BEGIN " + "OPENSSH PRIVATE KEY-----"] + body + ["-----END " + "OPENSSH PRIVATE KEY-----"])
    expect("deploy/id_ed25519", "private-key", 1)
elif stage == "c4":
    write("testdata/slack.env", ["SLACK_BOT_TOKEN=xox" + "b-" + rand(string.digits, 12) + "-" + rand(string.digits, 12) + "-" + rand(ALNUM, 23) + "7"])
elif stage == "c5":
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64url(json.dumps({"sub": "cc-smoke", "iat": 1759309200}, separators=(",", ":")).encode())
    signature = b64url(hmac.new(os.urandom(32), (header + "." + payload).encode(), hashlib.sha256).digest())
    write("src/settings.py", [
        'DB_PASSWORD = "' + rand(ALNUM + "!#", 17) + secrets.choice(string.digits) + "!" + secrets.choice(string.ascii_letters) + '"',
        'SESSION_TOKEN = "' + header + "." + payload + "." + signature + '"',
        'PLACEHOLDER_PASSWORD = "${DB_PASSWORD}"',
        'weak_password = "hunter2hunter2"',
        'IGNORED_TOKEN = "' + rand(ALNUM, 23) + secrets.choice(string.digits) + '"  # gitleaks:allow',
        'ALERTS_WEBHOOK = "' + webhook() + '"',
    ])
    expect("src/settings.py", "generic-password", 1)
    expect("src/settings.py", "jwt", 2)
    expect("src/settings.py", "slack-webhook-url", 6)
elif stage == "c6":
    write("docs/notes.md", ["# Release notes", "", "Nothing secret here."])
    token = "ghp" + "_" + rand(ALNUM, 35) + secrets.choice(string.digits)
    with open(os.path.join(os.path.dirname(state_path), "message.txt"), "w", encoding="utf-8") as handle:
        handle.write("Debug the release job\n\nTemporary token for the run: " + token + "\n")
    expect("", "github-pat", 3, "message")
elif stage == "c7":
    token = "github_pat_" + rand(ALNUM, 21) + secrets.choice(string.digits) + "_" + rand(ALNUM, 59)
    write("scripts/release.sh", ["#!/bin/sh", "export GITHUB_TOKEN=" + token, 'gh release create "$1"'])
    expect("scripts/release.sh", "github-fine-grained-pat", 2)
elif stage == "c8":
    write("config/new.env", ["AWS_ACCESS_KEY_ID=" + aws_key()])
    expect("config/new.env", "aws-access-token", 1)
elif stage == "staged":
    write("src/new_client.py", ["import json", 'SLACK_HOOK = "' + webhook() + '"'])
    expect("src/new_client.py", "slack-webhook-url", 2)
else:
    raise SystemExit("unknown stage " + stage)
with open(state_path, "w", encoding="utf-8") as handle:
    json.dump(state, handle, indent=1)
PY

cat > "$WORK/check.py" <<'PY'
import json, sys

state_path, commits_path, mode = sys.argv[1:4]
args = sys.argv[4:]
state = json.load(open(state_path))
commits = dict(line.split() for line in open(commits_path) if line.strip())


def norm(path):
    return path[2:] if path.startswith("./") else path


def load(path):
    if path == "-":
        return None
    data = json.load(open(path))
    return data["findings"] if isinstance(data, dict) else data


def mini(findings, where="file"):
    return {(f["commit"], norm(f["file"]), f["rule"], f["line"]) for f in findings if f["where"] == where}


def gitleaks(findings):
    return {(f["Commit"], norm(f["File"]), f["RuleID"], f["StartLine"]) for f in findings}


def expected(stages, where="file", with_commit=True):
    rows = set()
    for e in state["expected"]:
        if e["stage"] in stages and e["where"] == where:
            rows.add((commits.get(e["stage"], "") if with_commit else "", e["file"], e["rule"], e["line"]))
    return rows


def show(rows):
    return "\n".join("    %s %-20s %-26s line %s" % ((c[:7] or "-------"), f or "(message)", r, l) for c, f, r, l in sorted(rows))


def same(label, want, mine, theirs):
    problems = []
    if mine != want:
        problems.append("minileaks %s:\n%s\n  want:\n%s" % (label, show(mine), show(want)))
    if theirs is not None and theirs != want:
        problems.append("gitleaks %s:\n%s\n  want:\n%s" % (label, show(theirs), show(want)))
    if problems:
        sys.exit("\n".join(problems))
    print("  %s: %d finding(s), identical%s" % (label, len(want), "" if theirs is None else " in minileaks and gitleaks"))
    print(show(want))


if mode == "history":
    ml, gl = load(args[0]), load(args[1])
    stages = args[2].split(",")
    same("history (commit, file, rule, line)", expected(stages), mini(ml), gitleaks(gl) if gl is not None else None)
    want_messages = expected(stages, "message")
    got_messages = {(c, "", r, l) for c, f, r, l in mini(ml, "message")}
    if got_messages != want_messages:
        sys.exit("minileaks commit-message findings:\n%s\n  want:\n%s" % (show(got_messages), show(want_messages)))
    print("  commit message: minileaks reports %s" % ", ".join("%s line %s in %s" % (r, l, c[:7]) for c, _, r, l in sorted(got_messages)))
    if gl is not None:
        message_commits = {c for c, _, _, _ in want_messages}
        leaked = [f for f in gl if f["Commit"] in message_commits]
        if leaked:
            sys.exit("gitleaks unexpectedly reported findings in the message-only commit: %s" % leaked)
        print("  commit message: gitleaks reports nothing there (it scans added lines only)")
        dates_ml = {(f["commit"], norm(f["file"]), f["rule"], f["line"]): f["date"] for f in ml if f["where"] == "file"}
        for f in gl:
            key = (f["Commit"], norm(f["File"]), f["RuleID"], f["StartLine"])
            if dates_ml.get(key) != f["Date"]:
                sys.exit("date differs for %s: minileaks %s, gitleaks %s" % (key, dates_ml.get(key), f["Date"]))
        print("  author dates agree (UTC, RFC 3339)")
elif mode == "dir":
    ml, gl = load(args[0]), load(args[1])
    stages = args[2].split(",")
    want = expected(stages, with_commit=False)
    same("working tree (file, rule, line)", want, mini(ml), gitleaks(gl) if gl is not None else None)
elif mode == "only":
    ml, gl = load(args[0]), load(args[1])
    stage, with_messages = args[2], args[3] == "messages"
    want = expected([stage], with_commit=stage in commits)
    mine = mini(ml) | ({(c, f, r, l) for c, f, r, l in mini(ml, "message")} if with_messages else set())
    same("only new findings (%s)" % stage, want, mine, gitleaks(gl) if gl is not None else None)
elif mode == "messages-new":
    ml = load(args[0])
    want = expected(args[1].split(",")) | expected(args[2].split(","), "message")
    mine = mini(ml) | mini(ml, "message")
    if mine != want:
        sys.exit("minileaks with a gitleaks baseline:\n%s\n  want:\n%s" % (show(mine), show(want)))
    print("  with gitleaks' report as baseline and messages on, the commit-message token is new to minileaks too:")
    print(show(want))
elif mode == "sarif":
    def results(path):
        runs = json.load(open(path))["runs"]
        rows = set()
        for result in runs[0]["results"]:
            physical = result["locations"][0].get("physicalLocation")
            if physical:
                rows.add((result["ruleId"], norm(physical["artifactLocation"]["uri"]), physical["region"]["startLine"]))
        return rows
    mine, theirs = results(args[0]), results(args[1])
    if mine != theirs:
        sys.exit("SARIF results differ:\n  minileaks %s\n  gitleaks  %s" % (sorted(mine), sorted(theirs)))
    print("  SARIF: %d physical result(s) with the same ruleId, uri and startLine" % len(mine))
elif mode == "empty":
    for path in args:
        if path != "-" and load(path):
            sys.exit("%s should have no findings: %s" % (path, load(path)))
    print("  no findings left")
elif mode == "suppressed":
    report = json.load(open(args[0]))
    s = report["suppressed"]
    need = {"inline": 1, "entropy": 1, "allowlist": 2, "duplicate": 2}
    short = {k: s[k] for k, v in need.items() if s[k] < v}
    if short or report["scanned"]["paths_skipped"] < 1:
        sys.exit("minileaks suppression counts too low: %s (paths_skipped %s)" % (s, report["scanned"]["paths_skipped"]))
    print("  minileaks suppressed: %s; %d fragment(s) skipped by path allowlist" % (", ".join("%s %d" % kv for kv in s.items()), report["scanned"]["paths_skipped"]))
else:
    raise SystemExit("unknown mode " + mode)
PY

echo "▸ minileaks offline: entropy, the rule table and its gitleaks export"
m entropy aaaa abcd hunter2hunter2 > "$WORK/entropy.txt"
grep -q '^0.000 ' "$WORK/entropy.txt" && grep -q '^2.000 ' "$WORK/entropy.txt" && grep -q '^2.807 ' "$WORK/entropy.txt" || fail "entropy values: $(cat "$WORK/entropy.txt")"
printf '{"allowlists": [{"description": "test fixtures", "paths": ["(?:^|/)testdata/"]}]}\n' > "$CFG"
m rules --config "$CFG" --format gitleaks > "$TOML" || fail "rules --format gitleaks: $(cat "$TOML")"
RULES="$(grep -c '^\[\[rules\]\]$' "$TOML")"
[[ "$RULES" == "12" ]] && grep -q "paths = \['''(?:^|/)testdata/'''\]" "$TOML" || fail "gitleaks export has $RULES rules or lacks the testdata allowlist:
$(cat "$TOML")"
echo "  $RULES rules and $(grep -c '^\[\[allowlists\]\]$' "$TOML") global allowlists exported to gitleaks TOML"

if have_gitleaks; then
  echo "▸ download gitleaks v$GITLEAKS_VERSION and verify its sha256 against the release's checksum file and the pinned digest"
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64|Linux-amd64) ARCH=linux_x64; PINNED="$GITLEAKS_SHA256_LINUX_X64" ;;
    Linux-aarch64|Linux-arm64) ARCH=linux_arm64; PINNED="$GITLEAKS_SHA256_LINUX_ARM64" ;;
    *) fail "no pinned gitleaks digest for $(uname -s)-$(uname -m); run with CC_SMOKE_LOCAL_ONLY=1" ;;
  esac
  ASSET="gitleaks_${GITLEAKS_VERSION}_${ARCH}.tar.gz"
  BASE="https://github.com/gitleaks/gitleaks/releases/download/v$GITLEAKS_VERSION"
  curl -fsSL --retry 3 -o "$WORK/dl/$ASSET" "$BASE/$ASSET" || fail "download $BASE/$ASSET"
  curl -fsSL --retry 3 -o "$WORK/dl/checksums.txt" "$BASE/gitleaks_${GITLEAKS_VERSION}_checksums.txt" || fail "download the checksum file"
  WANT="$(awk -v f="$ASSET" '$2 == f {print $1}' "$WORK/dl/checksums.txt")"
  GOT="$(sha256_of "$WORK/dl/$ASSET")"
  [[ -n "$WANT" && "$WANT" == "$GOT" && "$GOT" == "$PINNED" ]] || fail "gitleaks checksum mismatch: checksum file '$WANT', pinned '$PINNED', downloaded '$GOT'"
  tar -xzf "$WORK/dl/$ASSET" -C "$WORK/dl/x"
  BINARY="$(find "$WORK/dl/x" -type f -name gitleaks -print -quit)"
  [[ -n "$BINARY" ]] || fail "no gitleaks binary in $ASSET: $(tar -tzf "$WORK/dl/$ASSET")"
  install -m 0755 "$BINARY" "$WORK/bin/gitleaks"
  gitleaks version > "$WORK/gitleaks-version.txt" 2>&1 || fail "gitleaks version: $(cat "$WORK/gitleaks-version.txt")"
  grep -q "$GITLEAKS_VERSION" "$WORK/gitleaks-version.txt" || fail "unexpected gitleaks version: $(cat "$WORK/gitleaks-version.txt")"
  echo "  gitleaks $(cat "$WORK/gitleaks-version.txt"), sha256 $GOT"
else
  echo "  (CC_SMOKE_LOCAL_ONLY=1: gitleaks and git-filter-repo are not downloaded; their comparisons are skipped)"
fi

echo "▸ build a throwaway repository with generated fake secrets (formats only, created now, never part of CloudCaptain)"
git init -q "$REPO"
plant c1 && git -C "$REPO" add -A && commit c1 "2026-10-01T09:00:00 +0000" -m "Add service config"
plant c2 && git -C "$REPO" add -A && commit c2 "2026-10-02T09:00:00 +0000" -m "Add deploy key"
git -C "$REPO" rm -q deploy/id_ed25519 && commit c3 "2026-10-03T09:00:00 +0000" -m "Remove deploy key"
plant c4 && git -C "$REPO" add -A && commit c4 "2026-10-04T09:00:00 +0000" -m "Add test fixtures"
plant c5 && git -C "$REPO" add -A && commit c5 "2026-10-05T09:00:00 +0000" -m "Add settings"
plant c6 && git -C "$REPO" add -A && commit c6 "2026-10-06T09:00:00 +0000" -F "$WORK/message.txt"
git -C "$REPO" checkout -q -b feature
plant c7 && git -C "$REPO" add -A && commit c7 "2026-10-07T09:00:00 +0000" -m "Add release script"
git -C "$REPO" checkout -q main
git -C "$REPO" log --all --format='  %h %ad %s' --date=short > "$WORK/log.txt"
cat "$WORK/log.txt"
GL_GIT="-"
GL_DIR="-"

echo "▸ history: minileaks git and gitleaks git find the same commits, files, rules and lines, including the deleted key and the feature branch"
run_expect 1 "$WORK/ml-git.json" m git "$REPO" --config "$CFG" --json
if have_gitleaks; then
  run_expect 1 "$WORK/gl-git.out" gitleaks git "$REPO" -c "$TOML" -f json -r "$WORK/gl-git.json"
  GL_GIT="$WORK/gl-git.json"
fi
check history "$WORK/ml-git.json" "$GL_GIT" c1,c2,c5,c6,c7 || fail "history scans disagree"

echo "▸ allowlists: the testdata path, the AWS example key, the placeholder, the weak password and the gitleaks:allow line are reported by neither"
check suppressed "$WORK/ml-git.json" || fail "suppression counts"
if have_gitleaks; then
  python3 - "$GL_GIT" <<'PY' || fail "gitleaks reported an allowlisted value"
import json, sys
findings = json.load(open(sys.argv[1]))
quiet_lines = {3, 4, 5}
bad = [f for f in findings if f["File"].startswith("testdata/") or "EXAMPLE" in f["Secret"] or (f["File"] == "src/settings.py" and f["StartLine"] in quiet_lines)]
if bad:
    sys.exit("allowlisted values reported by gitleaks: %s" % [(f["RuleID"], f["File"], f["StartLine"]) for f in bad])
print("  gitleaks: nothing under testdata/, no EXAMPLE key, nothing on the placeholder, weak-password or gitleaks:allow lines")
PY
fi

echo "▸ working tree: minileaks dir and gitleaks dir agree; .git, testdata/ and the deleted key are not reported"
run_expect 1 "$WORK/ml-dir.json" in_repo m dir . --config "$CFG" --json
if have_gitleaks; then
  run_expect 1 "$WORK/gl-dir.out" in_repo gitleaks dir . -c "$TOML" -f json -r "$WORK/gl-dir.json"
  GL_DIR="$WORK/gl-dir.json"
fi
check dir "$WORK/ml-dir.json" "$GL_DIR" c1,c5 || fail "working-tree scans disagree"

if have_gitleaks; then
  echo "▸ SARIF: both tools emit the same ruleId, uri and startLine for every file finding"
  run_expect 1 "$WORK/ml-git.sarif" m git "$REPO" --config "$CFG" --sarif
  run_expect 1 "$WORK/gl-git-sarif.out" gitleaks git "$REPO" -c "$TOML" -f sarif -r "$WORK/gl-git.sarif"
  check sarif "$WORK/ml-git.sarif" "$WORK/gl-git.sarif" || fail "SARIF reports disagree"
  python3 - "$WORK/ml-git.sarif" <<'PY' || fail "minileaks SARIF is not 2.1.0"
import json, sys
sarif = json.load(open(sys.argv[1]))
assert sarif["version"] == "2.1.0" and sarif["runs"][0]["tool"]["driver"]["name"] == "minileaks"
logical = [r for r in sarif["runs"][0]["results"] if "logicalLocations" in r["locations"][0]]
assert len(logical) == 1, logical
print("  minileaks SARIF: version 2.1.0, the commit-message finding as a logical location")
PY
fi

echo "▸ baseline: after one more leaky commit, both tools report only the new finding"
plant c8 && git -C "$REPO" add -A && commit c8 "2026-10-08T09:00:00 +0000" -m "Add the new region"
GL_NEW="-"
run_expect 1 "$WORK/ml-new.json" m git "$REPO" --config "$CFG" --baseline "$WORK/ml-git.json" --json
if have_gitleaks; then
  run_expect 1 "$WORK/gl-new.out" gitleaks git "$REPO" -c "$TOML" --baseline-path "$GL_GIT" -f json -r "$WORK/gl-new.json"
  GL_NEW="$WORK/gl-new.json"
fi
check only "$WORK/ml-new.json" "$GL_NEW" c8 messages || fail "baseline runs disagree"
if have_gitleaks; then
  run_expect 1 "$WORK/ml-new-gl.json" m git "$REPO" --config "$CFG" --baseline "$GL_GIT" --no-messages --json
  check only "$WORK/ml-new-gl.json" - c8 files || fail "minileaks with gitleaks' report as its baseline"
  echo "  minileaks accepts gitleaks' JSON report as a baseline"
  run_expect 1 "$WORK/ml-new-gl-msg.json" m git "$REPO" --config "$CFG" --baseline "$GL_GIT" --json
  check messages-new "$WORK/ml-new-gl-msg.json" c8 c6 || fail "message findings with a gitleaks baseline"
fi

echo "▸ pre-commit mode: a newly staged secret is caught by minileaks staged and gitleaks git --pre-commit --staged"
plant staged && git -C "$REPO" add src/new_client.py
GL_STAGED="-"
run_expect 1 "$WORK/ml-staged.json" m staged "$REPO" --config "$CFG" --json
if have_gitleaks; then
  run_expect 1 "$WORK/gl-staged.out" gitleaks git "$REPO" --pre-commit --staged -c "$TOML" -f json -r "$WORK/gl-staged.json"
  GL_STAGED="$WORK/gl-staged.json"
fi
check only "$WORK/ml-staged.json" "$GL_STAGED" staged files || fail "staged scans disagree"

echo "▸ the installed hook blocks the commit, and lets a clean one through"
m install-hook "$REPO" > "$WORK/hook.txt" || fail "install-hook: $(cat "$WORK/hook.txt")"
[[ -x "$REPO/.git/hooks/pre-commit" ]] || fail "hook not executable: $(ls -l "$REPO/.git/hooks")"
BEFORE="$(git -C "$REPO" rev-parse HEAD)"
CODE=0
git -C "$REPO" commit -q -m "Add the client" > "$WORK/hook-blocked.txt" 2>&1 || CODE=$?
[[ "$CODE" != "0" && "$(git -C "$REPO" rev-parse HEAD)" == "$BEFORE" ]] || fail "the hook should block the commit (exit $CODE): $(cat "$WORK/hook-blocked.txt")"
grep -q "slack-webhook-url" "$WORK/hook-blocked.txt" || fail "hook output should name the rule: $(cat "$WORK/hook-blocked.txt")"
echo "  blocked: $(grep -m1 'slack-webhook-url' "$WORK/hook-blocked.txt" | tr -s ' ')"
git -C "$REPO" rm -q --cached src/new_client.py
rm -f "$REPO/src/new_client.py"
printf 'print("ok")\n' > "$REPO/src/clean.py"
git -C "$REPO" add src/clean.py
commit clean "2026-10-09T09:00:00 +0000" -m "Add a clean module" > "$WORK/hook-clean.txt" 2>&1 || fail "a clean commit should pass the hook: $(cat "$WORK/hook-clean.txt")"
[[ "$(git -C "$REPO" rev-parse HEAD)" != "$BEFORE" ]] || fail "the clean commit did not land"
echo "  clean commit $(git -C "$REPO" rev-parse --short HEAD) passed the hook"

if ! have_gitleaks; then
  echo "SMOKE OK: security/secret-scanning (local only: minileaks without gitleaks and git-filter-repo)"
  exit 0
fi

echo "▸ rotate first, then rewrite: git filter-repo --sensitive-data-removal on a fresh clone of a pushed remote"
ORIGIN="$WORK/origin.git"
git init -q --bare "$ORIGIN"
git -C "$REPO" remote add origin "$ORIGIN"
git -C "$REPO" push -q origin --all
OLD="$(awk '$1 == "c1" {print $2}' "$COMMITS")"
run_expect 1 "$WORK/ml-before.json" m git "$REPO" --config "$CFG" --json --replace-text "$WORK/expressions.txt"
MODE="$(python3 -c 'import os, stat, sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))' "$WORK/expressions.txt")"
[[ "$MODE" == "0o600" ]] || fail "expressions file mode is $MODE, want 0o600"
echo "  $(wc -l < "$WORK/expressions.txt" | tr -d ' ') filter-repo expressions written with mode 0600 (they contain the secrets)"
python3 -m venv "$WORK/venv" || fail "python3 -m venv"
printf 'git-filter-repo==%s --hash=sha256:%s\n' "$FILTER_REPO_VERSION" "$FILTER_REPO_WHEEL_SHA256" > "$WORK/requirements.txt"
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check --retries 3 --require-hashes --only-binary=:all: -r "$WORK/requirements.txt" > "$WORK/pip.txt" 2>&1 || fail "pip install git-filter-repo: $(cat "$WORK/pip.txt")"
export PATH="$WORK/venv/bin:$PATH"
git filter-repo --version > "$WORK/filter-repo-version.txt" 2>&1 || fail "git filter-repo --version: $(cat "$WORK/filter-repo-version.txt")"
echo "  git-filter-repo $FILTER_REPO_VERSION installed with a pinned wheel hash ($(cat "$WORK/filter-repo-version.txt"))"
git clone -q --no-local "$ORIGIN" "$WORK/cleanup"
(cd "$WORK/cleanup" && git filter-repo --sensitive-data-removal --replace-text "$WORK/expressions.txt" --replace-message "$WORK/expressions.txt" < /dev/null) > "$WORK/filter-repo.txt" 2>&1 || fail "git filter-repo: $(cat "$WORK/filter-repo.txt")"
grep -q "refs/heads/feature" "$WORK/cleanup/.git/filter-repo/changed-refs" || fail "the feature branch was not rewritten: $(cat "$WORK/cleanup/.git/filter-repo/changed-refs")"
git -C "$WORK/cleanup" push -q --force --mirror origin > "$WORK/push.txt" 2>&1 || fail "git push --force --mirror: $(cat "$WORK/push.txt")"
echo "  rewritten refs: $(tr '\n' ' ' < "$WORK/cleanup/.git/filter-repo/changed-refs")"

echo "▸ the remote is clean for both scanners, the old objects survive until gc, and the developer's old clone still leaks"
run_expect 0 "$WORK/ml-after.json" m git "$ORIGIN" --config "$CFG" --json
run_expect 0 "$WORK/gl-after.out" gitleaks git "$ORIGIN" -c "$TOML" -f json -r "$WORK/gl-after.json"
check empty "$WORK/ml-after.json" "$WORK/gl-after.json" || fail "findings left after the rewrite"
git -C "$ORIGIN" --no-replace-objects cat-file -e "$OLD^{commit}" || fail "the old commit should still be in the remote's object store before gc"
echo "  old commit ${OLD:0:7} is unreachable but still stored in the remote"
git -C "$ORIGIN" reflog expire --expire=now --all
git -C "$ORIGIN" gc -q --prune=now
if git -C "$ORIGIN" --no-replace-objects cat-file -e "$OLD^{commit}" 2>/dev/null; then
  fail "the old commit survived gc --prune=now"
fi
echo "  after gc --prune=now it is gone; on GitHub that step needs GitHub Support"
run_expect 1 "$WORK/ml-old-clone.json" m git "$REPO" --config "$CFG" --json
echo "  the original clone still reports $(python3 -c 'import json, sys; print(len(json.load(open(sys.argv[1]))["findings"]))' "$WORK/ml-old-clone.json") finding(s): only revocation makes those copies harmless"

echo "SMOKE OK: security/secret-scanning"
