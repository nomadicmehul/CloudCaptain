#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
LESSON="$PWD"
MH="$LESSON/code/main.py"
PRE_COMMIT_VERSION="4.7.0"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
export GIT_CONFIG_GLOBAL="$WORK/gitconfig" GIT_CONFIG_NOSYSTEM=1
git config --global user.name "Smoke Bot"
git config --global user.email "smoke@example.invalid"
git config --global init.defaultBranch main
git config --global commit.gpgsign false
git config --global advice.detachedHead false
export GIT_AUTHOR_DATE="1790000000 +0000" GIT_COMMITTER_DATE="1790000000 +0000"
export PRE_COMMIT_HOME="$WORK/pre-commit-home" PRE_COMMIT_COLOR=never SKIP=

echo "▸ $(git version), $(python3 --version)"

echo "▸ install pre-commit $PRE_COMMIT_VERSION and its dependencies from PyPI with pinned sha256 hashes"
python3 -m venv "$WORK/venv" || fail "python3 -m venv"
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check --retries 3 --require-hashes --only-binary=:all: \
  -r "$LESSON/code/requirements-pre-commit.txt" > "$WORK/pip.txt" 2>&1 || fail "pip install pre-commit: $(cat "$WORK/pip.txt")"
PRE_COMMIT="$WORK/venv/bin/pre-commit"
[[ "$("$PRE_COMMIT" --version)" == "pre-commit $PRE_COMMIT_VERSION" ]] || fail "unexpected version: $("$PRE_COMMIT" --version)"

echo "▸ two identical sample repositories: A gets pre-commit, B gets minihooks, from the same .pre-commit-config.yaml"
python3 "$MH" demo "$WORK/a" --no-install > /dev/null || fail "minihooks demo a"
python3 "$MH" demo "$WORK/b" --no-install > /dev/null || fail "minihooks demo b"
A="$WORK/a/work"
B="$WORK/b/work"
export PATH="$WORK/b/bin:$PATH"
[[ "$(git -C "$A" rev-parse HEAD)" == "$(git -C "$B" rev-parse HEAD)" ]] || fail "the two sample repositories differ"
(cd "$A" && "$PRE_COMMIT" install --hook-type pre-commit --hook-type commit-msg) > "$WORK/install-a.txt" 2>&1 || fail "pre-commit install: $(cat "$WORK/install-a.txt")"
python3 "$MH" install --repo "$B" -t pre-commit -t commit-msg > "$WORK/install-b.txt" 2>&1 || fail "minihooks install: $(cat "$WORK/install-b.txt")"
sed 's/^/  /' "$WORK/install-a.txt" "$WORK/install-b.txt"

cat > "$WORK/compare.py" <<'PY'
import hashlib, json, os, re, subprocess, sys

work, step, want_rc, want = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
line = re.compile(r"^(?P<name>.+?)\.{2,}(?:\(no files to check\))?(?P<status>Passed|Failed|Skipped)$")


def state(repo):
    git = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True).stdout
    files = sorted(set(git("ls-files", "-co", "--exclude-standard", "-z").split("\0")) - {""})
    content = {}
    for name in files:
        path = os.path.join(repo, name)
        content[name] = hashlib.sha256(open(path, "rb").read()).hexdigest()[:12] if os.path.exists(path) else None
    return {"head": git("rev-parse", "HEAD^{tree}").strip(), "index": git("write-tree").strip(), "work": content,
            "status": sorted(git("status", "--porcelain").splitlines())}


seen = {}
for tool, repo in (("pre-commit", "a/work"), ("minihooks", "b/work")):
    text = open(os.path.join(work, f"{step}.{tool}.out"), encoding="utf-8", errors="replace").read()
    rc = int(open(os.path.join(work, f"{step}.{tool}.rc")).read())
    hooks = {m.group("name"): m.group("status") for m in map(line.match, text.splitlines()) if m}
    seen[tool] = {"rc": rc, "hooks": hooks, "state": state(os.path.join(work, repo)), "text": text}
p, m = seen["pre-commit"], seen["minihooks"]
problems = []
if p["hooks"] != m["hooks"]:
    problems.append(f"hook results differ: pre-commit {p['hooks']} minihooks {m['hooks']}")
if p["hooks"] != want:
    problems.append(f"pre-commit hook results {p['hooks']} are not the expected {want}")
if (p["rc"] == 0) != (m["rc"] == 0) or (p["rc"] == 0) != (want_rc == "0"):
    problems.append(f"exit codes: pre-commit {p['rc']}, minihooks {m['rc']}, expected {'0' if want_rc == '0' else 'non-zero'}")
if p["state"] != m["state"]:
    problems.append(f"repository state differs:\n  pre-commit {json.dumps(p['state'], sort_keys=True)}\n  minihooks  {json.dumps(m['state'], sort_keys=True)}")
if problems:
    print("\n".join(problems))
    print("--- pre-commit output\n" + p["text"] + "--- minihooks output\n" + m["text"])
    sys.exit(1)
failed = sorted(name for name, status in p["hooks"].items() if status == "Failed")
print(f"  {step}: both exit {'0' if p['rc'] == 0 else 'non-zero'}; failed: {', '.join(failed) or 'none'}; index, HEAD and working tree identical")
PY

both() {
  local repo
  for repo in "$A" "$B"; do (cd "$repo" && eval "$1") || fail "setting up both repositories: $1"; done
}
commit_both() {
  local step="$1" message="$2" tool repo rc
  for tool in pre-commit minihooks; do
    if [[ "$tool" == pre-commit ]]; then repo="$A"; else repo="$B"; fi
    rc=0
    git -C "$repo" commit -m "$message" > "$WORK/$step.$tool.out" 2>&1 || rc=$?
    echo "$rc" > "$WORK/$step.$tool.rc"
  done
}
run_both() {
  local step="$1" rc
  rc=0; (cd "$A" && "$PRE_COMMIT" run --all-files) > "$WORK/$step.pre-commit.out" 2>&1 || rc=$?; echo "$rc" > "$WORK/$step.pre-commit.rc"
  rc=0; (cd "$B" && python3 "$MH" run --all-files) > "$WORK/$step.minihooks.out" 2>&1 || rc=$?; echo "$rc" > "$WORK/$step.minihooks.rc"
}
check() {
  python3 "$WORK/compare.py" "$WORK" "$1" "$2" "$3" > "$WORK/compare.txt" 2>&1 || fail "$1: $(cat "$WORK/compare.txt")"
  cat "$WORK/compare.txt"
}
both_same() {
  local what="$1" expected="$2" repo got
  for repo in "$A" "$B"; do
    got="$(cd "$repo" && eval "$what")"
    [[ "$got" == "$expected" ]] || fail "$repo: '$what' gave '$got', expected '$expected'"
  done
}

FIXED_APP=$'def total(items):\n    return sum(items, 0)\n\n\n\n\n\n\n\ndef count(items):\n    return len(items)\n'
echo "▸ a fixer on a staged file while other edits stay unstaged: both block the commit and keep the unstaged edits"
both "printf 'def total(items):   \n    return sum(items, 0)\n\n\n\n\n\n\n\ndef count(items):\n    return len(items)\n' > app.py && git add app.py"
both "printf 'def total(items):   \n    return sum(items, 0)\n\n\n\n\n\n\n\ndef count(items):\n    return len(items)  # unstaged\n' > app.py"
both "printf 'unstaged README line\n' >> README.md"
commit_both s1 "feat: count items"
check s1 1 '{"trim trailing whitespace": "Failed", "check json syntax": "Skipped", "block secrets": "Passed", "forbid .env files": "Skipped"}'
both_same "tail -n 1 app.py" "    return len(items)  # unstaged"
both_same "sed -n 1p app.py" "def total(items):"
both_same "tail -n 1 README.md" "unstaged README line"
both_same "git show :app.py | sed -n 1p" "def total(items):   "
both_same "git rev-list --count HEAD" "1"
grep -q "Unstaged files detected" "$WORK/s1.pre-commit.out" && grep -q "Unstaged files detected" "$WORK/s1.minihooks.out" || fail "both should report the stash: $(cat "$WORK/s1.minihooks.out")"

echo "▸ the fix is staged, the message is not conventional: the commit-msg hook blocks in both"
both "printf '%s' \"\$FIXED_APP\" > app.py && git add app.py"
commit_both s2 "counted items"
check s2 1 '{"trim trailing whitespace": "Passed", "check json syntax": "Skipped", "block secrets": "Passed", "forbid .env files": "Skipped", "conventional commit message": "Failed"}'
both_same "git rev-list --count HEAD" "1"

echo "▸ invalid JSON, a planted AWS key and a .env file: the same three hooks fail in both"
FAKE_KEY="AKIA""IOSFODNN7EXAMPLE"
export FAKE_KEY
both "printf '{\"workers\": 8,}\n' > settings.json && printf 'aws_access_key_id = %s\n' \"\$FAKE_KEY\" > keys.txt && printf 'TOKEN=1\n' > .env && git add settings.json keys.txt .env"
commit_both s3 "feat: add settings"
check s3 1 '{"trim trailing whitespace": "Passed", "check json syntax": "Failed", "block secrets": "Failed", "forbid .env files": "Failed"}'
grep -q "keys.txt:1:aws_access_key_id" "$WORK/s3.pre-commit.out" && grep -q "keys.txt:1:aws_access_key_id" "$WORK/s3.minihooks.out" || fail "pygrep output differs: $(cat "$WORK/s3.minihooks.out")"

echo "▸ a fix that collides with an unstaged edit on the same line: both roll the fix back and restore the edit"
both "git rm -q --cached keys.txt .env && rm keys.txt .env && printf '{\"workers\": 8}\n' > settings.json && git add settings.json"
both "printf 'def total(items):\n    return sum(items, 0)   \n\n\n\n\n\n\n\ndef count(items):\n    return len(items)\n' > app.py && git add app.py"
both "printf 'def total(items):\n    return sum(items, start)   \n\n\n\n\n\n\n\ndef count(items):\n    return len(items)\n' > app.py"
commit_both s4 "feat: count items"
check s4 1 '{"trim trailing whitespace": "Failed", "check json syntax": "Passed", "block secrets": "Passed", "forbid .env files": "Skipped"}'
both_same "sed -n 2p app.py" "    return sum(items, start)   "
both_same "git show :app.py | sed -n 2p" "    return sum(items, 0)   "
grep -q "Rolling back fixes" "$WORK/s4.pre-commit.out" && grep -q "Rolling back fixes" "$WORK/s4.minihooks.out" || fail "both should report the rollback: $(cat "$WORK/s4.minihooks.out")"

echo "▸ everything fixed and re-staged: both commit, the same tree lands, README's unstaged line is still there"
both "printf '%s' \"\$FIXED_APP\" > app.py && git add app.py"
commit_both s5 "feat(app): count items and raise workers"
check s5 0 '{"trim trailing whitespace": "Passed", "check json syntax": "Passed", "block secrets": "Passed", "forbid .env files": "Skipped", "conventional commit message": "Passed"}'
both_same "git rev-list --count HEAD" "2"
both_same "git log -1 --format=%s" "feat(app): count items and raise workers"
both_same "tail -n 1 README.md" "unstaged README line"

echo "▸ pre-commit run --all-files and minihooks run --all-files agree, before and after a bypassed commit"
run_both s6
check s6 0 '{"trim trailing whitespace": "Passed", "check json syntax": "Passed", "block secrets": "Passed", "forbid .env files": "Skipped"}'
both "printf 'Trailing   \n' > NOTES.md && git add NOTES.md && git commit -q --no-verify -m 'docs: notes'"
run_both s7
check s7 1 '{"trim trailing whitespace": "Failed", "check json syntax": "Passed", "block secrets": "Passed", "forbid .env files": "Skipped"}'
both "git checkout -q -- NOTES.md"

echo "▸ core.hooksPath: pre-commit refuses to install, minihooks installs where Git will look, and Git runs it"
python3 "$MH" demo "$WORK/c" --no-install > /dev/null
C="$WORK/c/work"
git -C "$C" config core.hooksPath .githooks
rc=0; (cd "$C" && "$PRE_COMMIT" install) > "$WORK/hookspath.txt" 2>&1 || rc=$?
[[ "$rc" != 0 ]] && grep -q "core.hooksPath" "$WORK/hookspath.txt" || fail "pre-commit install with core.hooksPath set should refuse: exit $rc: $(cat "$WORK/hookspath.txt")"
python3 "$MH" install --repo "$C" > "$WORK/hookspath.txt" || fail "minihooks install with core.hooksPath"
[[ -x "$C/.githooks/pre-commit" && ! -e "$C/.git/hooks/pre-commit" ]] || fail "hook not in .githooks: $(cat "$WORK/hookspath.txt")"
printf 'x = 1   \n' > "$C/extra.py"; git -C "$C" add extra.py
rc=0; git -C "$C" commit -m "feat: extra" > "$WORK/hookspath.txt" 2>&1 || rc=$?
[[ "$rc" != 0 ]] && grep -q "trim trailing whitespace.*Failed" "$WORK/hookspath.txt" || fail "git did not run the hook from core.hooksPath: $(cat "$WORK/hookspath.txt")"
echo "  pre-commit install refused (core.hooksPath is set); minihooks wrote .githooks/pre-commit and git ran it"

echo "▸ server side: B's bare origin runs minihooks pre-receive (policy in its git config)"
ORIGIN="$WORK/b/origin.git"
[[ -x "$ORIGIN/hooks/pre-receive" ]] || fail "no pre-receive hook in $ORIGIN"
git -C "$ORIGIN" config --get-all minihooks.protect | sed 's/^/  protect /'
SIGNING=0
if command -v ssh-keygen >/dev/null; then
  SIGNING=1
  ssh-keygen -q -t ed25519 -N "" -C smoke -f "$WORK/signing_key"
  echo "smoke@example.invalid $(cat "$WORK/signing_key.pub")" > "$WORK/allowed_signers"
  git -C "$ORIGIN" config gpg.ssh.allowedSignersFile "$WORK/allowed_signers"
  git -C "$ORIGIN" config minihooks.requireSigned true
  git -C "$B" config gpg.format ssh
  git -C "$B" config user.signingkey "$WORK/signing_key"
  echo "  signed commits required on protected refs (SSH key, allowed signers on the server)"
else
  echo "  (ssh-keygen not installed; the signed-commit checks are skipped)"
fi
OLD_MAIN="$(git -C "$ORIGIN" rev-parse main)"
git -C "$B" checkout -q -- README.md
echo "  B's main holds $(git -C "$B" rev-list --count "$OLD_MAIN..main") unsigned commits the server has not seen"

push() {
  local name="$1" want="$2" rc=0; shift 2
  git -C "$B" push origin "$@" > "$WORK/push-$name.txt" 2>&1 || rc=$?
  if [[ "$want" == accept ]]; then
    [[ "$rc" == 0 ]] || fail "push $name should be accepted: $(cat "$WORK/push-$name.txt")"
    echo "  $name: accepted"
  else
    [[ "$rc" != 0 ]] && grep -q "pre-receive hook declined" "$WORK/push-$name.txt" || fail "push $name should be declined by pre-receive (exit $rc): $(cat "$WORK/push-$name.txt")"
    grep -q "remote: minihooks: .* $want: " "$WORK/push-$name.txt" || fail "push $name: expected a '$want' violation: $(cat "$WORK/push-$name.txt")"
    echo "  $name: rejected, $(grep -m1 -o "minihooks: .* $want: .*" "$WORK/push-$name.txt" | sed "s/ *$//")"
  fi
}
commit_b() { git -C "$B" add -A && git -C "$B" commit -q --no-verify "$@"; }

if [[ "$SIGNING" == 1 ]]; then
  push unsigned-main unsigned main
  [[ "$(git -C "$ORIGIN" rev-parse main)" == "$OLD_MAIN" ]] || fail "origin main moved after a rejected push"
  git -C "$B" rebase -q --force-rebase --gpg-sign "$OLD_MAIN" > "$WORK/rebase.txt" 2>&1 || fail "re-signing with git rebase --gpg-sign: $(cat "$WORK/rebase.txt")"
fi
push signed-main accept main
GOOD_MAIN="$(git -C "$B" rev-parse main)"
[[ "$(git -C "$ORIGIN" rev-parse main)" == "$GOOD_MAIN" ]] || fail "origin main is not the pushed commit"
if [[ "$SIGNING" == 1 ]]; then
  git -C "$ORIGIN" verify-commit main > "$WORK/verify.txt" 2>&1 || fail "git verify-commit on origin: $(cat "$WORK/verify.txt")"
fi

git -C "$B" checkout -q -b feature/assets
head -c 2097152 /dev/zero > "$B/big.bin"
commit_b -m "feat: add assets"
BIG="$(git -C "$B" rev-parse HEAD)"
push big-file large-file feature/assets
grep -q "big.bin is 2097152 bytes" "$WORK/push-big-file.txt" || fail "size in the message: $(cat "$WORK/push-big-file.txt")"
[[ "$(git -C "$B" cat-file -s HEAD:big.bin)" == 2097152 ]] || fail "git cat-file -s disagrees with the reported size"
git -C "$ORIGIN" cat-file -e "$BIG" 2>/dev/null && fail "a rejected commit landed in origin's object store"
git -C "$ORIGIN" rev-parse -q --verify refs/heads/feature/assets > /dev/null && fail "the rejected branch exists on origin"
echo "  the rejected objects stayed in the quarantine and were deleted with it"

git -C "$B" checkout -q -b feature/creds main
printf 'aws_access_key_id = %s\n' "$FAKE_KEY" > "$B/creds.ini"
commit_b -m "feat: add credentials"
push secret secret feature/creds
grep -q "creds.ini:1 looks like a aws-access-key-id" "$WORK/push-secret.txt" || fail "secret location: $(cat "$WORK/push-secret.txt")"

git -C "$B" checkout -q -b feature/wip main
printf 'draft\n' > "$B/draft.md"
commit_b -m "wip"
push wip-branch accept feature/wip

git -C "$B" checkout -q main
printf '\nMore words.\n' >> "$B/README.md"
if [[ "$SIGNING" == 1 ]]; then commit_b -S -m "wip"; else commit_b -m "wip"; fi
push wip-main conventional-commit main
if [[ "$SIGNING" == 1 ]]; then
  git -C "$B" commit -q --amend --no-verify --no-gpg-sign -m "docs: more words"
  push unsigned-docs unsigned main
fi
git -C "$B" reset -q --hard "$GOOD_MAIN"
push rewrite non-fast-forward --force "$GOOD_MAIN~1:main"
push delete delete-protected :main
[[ "$(git -C "$ORIGIN" rev-parse main)" == "$GOOD_MAIN" ]] || fail "origin main changed: $(git -C "$ORIGIN" log --oneline -3 main)"
[[ "$(git -C "$ORIGIN" for-each-ref --format='%(refname)' refs/heads | tr '\n' ' ')" == "refs/heads/feature/wip refs/heads/main " ]] || fail "origin refs: $(git -C "$ORIGIN" for-each-ref refs/heads)"
git -C "$ORIGIN" fsck --strict --no-progress > "$WORK/fsck.txt" 2>&1 || fail "git fsck --strict on origin: $(cat "$WORK/fsck.txt")"

echo "▸ the same policy, run by hand against a list of updates (what Git feeds the hook on stdin)"
git -C "$B" branch -q -D feature/assets
printf '%s %s refs/heads/main\n' "$GOOD_MAIN" "$BIG" > "$WORK/updates.txt"
rc=0; python3 "$MH" pre-receive --repo "$B" --json --max-bytes 1000000 < "$WORK/updates.txt" > "$WORK/policy.json" 2> "$WORK/policy.err" || rc=$?
[[ "$rc" == 1 ]] || fail "pre-receive by hand should exit 1: $rc $(cat "$WORK/policy.err")"
python3 - "$WORK/policy.json" <<'PY' || fail "pre-receive JSON: $(cat "$WORK/policy.json")"
import json, sys
rules = sorted({item["rule"] for item in json.load(open(sys.argv[1]))})
assert rules == ["large-file"], rules
print("  pre-receive --json on an unreachable commit: large-file only (conventional, fast-forward, signatures not required by default)")
PY

echo "SMOKE OK: git/git-hooks"
