#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
LESSON="$PWD"
MQ="$LESSON/code/main.py"
FIXTURES="$LESSON/code/fixtures"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
export GIT_CONFIG_GLOBAL="$WORK/gitconfig" GIT_CONFIG_NOSYSTEM=1
git config --global user.name "Smoke Bot"
git config --global user.email "smoke@example.invalid"
git config --global init.defaultBranch main
git config --global commit.gpgsign false
git config --global core.ignorecase false

echo "▸ $(git version), $(python3 --version)"
python3 - "$(git version)" <<'PY' || fail "mergequeue needs git 2.40 or later (merge-tree --write-tree with --merge-base): $(git version)"
import re, sys
found = re.search(r"(\d+)\.(\d+)", sys.argv[1])
sys.exit(0 if found and tuple(map(int, found.groups())) >= (2, 40) else 1)
PY

lines_of() {
  python3 - "$1" <<'PY'
import json, sys
result = {}
for raw in open(sys.argv[1], encoding="utf-8"):
    where, path = raw.rstrip("\n").split("\t", 1)
    result[path] = None if where.startswith("::") else int(where.split(":")[1])
print(json.dumps(result, sort_keys=True))
PY
}

make_tree() {
  local dir="$1"; shift
  rm -rf "$dir"; mkdir -p "$dir"; git -C "$dir" init -q
  printf '%s\n' "$@" > "$dir.paths"
  local p
  for p in "$@"; do mkdir -p "$dir/$(dirname "$p")"; echo x > "$dir/$p"; done
}

check_ignore() {
  local dir="$1" out="$2" rc=0
  git -C "$dir" check-ignore --no-index -v -n --stdin < "$dir.paths" > "$out" || rc=$?
  [[ "$rc" -le 1 ]] || fail "git check-ignore failed with exit $rc in $dir"
}

echo "▸ CODEOWNERS patterns agree with git check-ignore on the subset they share, one pattern at a time"
SHARED_PATHS=(a.js apps/x.js apps/github/y.rb src/apps/z.go docs/getting-started.md docs/build-app/troubleshooting.md src/docs/intro.md
  build/logs/a.log deep/nested/logs/b.txt README.md pkg/README.md a/b a/x/y/b a/bx/c.txt src/test_app.py src/core/deep/test_core.py
  src/core/app.py x.txt xy.txt config/prod.ini config/env/dev.ini lib/util.py)
SHARED_PATTERNS=('*' '*.js' '/docs/' 'docs/' 'apps/' '/apps/github' '**/logs' 'docs/**' 'src/**/test_*.py' '/build/logs/' 'README.md' 'a/**/b' '?.txt' '/config/*.ini' 'lib/*.py')
make_tree "$WORK/shared" "${SHARED_PATHS[@]}"
for pattern in "${SHARED_PATTERNS[@]}"; do
  printf '%s\n' "$pattern" > "$WORK/shared/.gitignore"
  check_ignore "$WORK/shared" "$WORK/git.txt"
  printf '%s @smoke\n' "$pattern" > "$WORK/one.CODEOWNERS"
  python3 "$MQ" owners --codeowners "$WORK/one.CODEOWNERS" --json --stdin < "$WORK/shared.paths" > "$WORK/mine.json" || fail "mergequeue owners for $pattern: $(cat "$WORK/mine.json")"
  python3 - "$WORK/mine.json" "$(lines_of "$WORK/git.txt")" "$pattern" <<'PY' || fail "pattern $pattern: CODEOWNERS and git check-ignore disagree"
import json, sys
mine = {path: rule is not None for path, rule in json.load(open(sys.argv[1]))["paths"].items()}
theirs = {path: line is not None for path, line in json.loads(sys.argv[2]).items()}
diff = {p: {"mergequeue": mine[p], "git": theirs.get(p)} for p in mine if mine[p] != theirs.get(p)}
assert not diff and set(mine) == set(theirs), f"{sys.argv[3]!r}: {diff}"
PY
done
echo "  ${#SHARED_PATTERNS[@]} patterns x ${#SHARED_PATHS[@]} paths: the same answer from mergequeue and git check-ignore"

echo "▸ owners --gitignore reproduces git check-ignore -v line for line (excluded directories, negation, character classes)"
FIXTURE_PATHS=(app/billing/refunds.py app/billing/migrations/0042_refunds.sql app/api.py app/legacy/old.py app/b2.py docs/billing.md
  src/docs/intro.md docs/internal/oncall.md README.md sub/README.md tools/lint.sh tools/ci/run.sh config/prod.ini Makefile scripts/db/seed.sql)
make_tree "$WORK/fixture" "${FIXTURE_PATHS[@]}"
awk '/^[ \t]*(#|$)/ {print ""; next} {print $1}' "$FIXTURES/CODEOWNERS" > "$WORK/fixture/.gitignore"
check_ignore "$WORK/fixture" "$WORK/git.txt"
python3 "$MQ" owners --codeowners "$FIXTURES/CODEOWNERS" --gitignore --stdin < "$WORK/fixture.paths" > "$WORK/mine.txt" || fail "owners --gitignore: $(cat "$WORK/mine.txt")"
[[ "$(lines_of "$WORK/git.txt")" == "$(lines_of "$WORK/mine.txt")" ]] || fail "fixture CODEOWNERS as .gitignore: git $(lines_of "$WORK/git.txt") mergequeue $(lines_of "$WORK/mine.txt")"
NEGATION_PATHS=(x.log keep.log logs/keep.log src/a1.py src/c1.py src/b2.go src/a2.go vendor/x/README.md docs/a.md docs/sub/b.md build/out/bin tmp/t a/b/tmp/t notes.txt)
make_tree "$WORK/negation" "${NEGATION_PATHS[@]}"
printf '%s\n' '*.log' '!keep.log' '/src/[ab]*.py' '/vendor/' 'docs/*' 'build/**' '**/tmp' '/src/[!a]?.go' > "$WORK/negation/.gitignore"
check_ignore "$WORK/negation" "$WORK/git.txt"
python3 "$MQ" owners --codeowners "$WORK/negation/.gitignore" --gitignore --stdin < "$WORK/negation.paths" > "$WORK/mine.txt" || fail "owners --gitignore on the negation file"
[[ "$(lines_of "$WORK/git.txt")" == "$(lines_of "$WORK/mine.txt")" ]] || fail "negation .gitignore: git $(lines_of "$WORK/git.txt") mergequeue $(lines_of "$WORK/mine.txt")"
echo "  $(( ${#FIXTURE_PATHS[@]} + ${#NEGATION_PATHS[@]} )) paths: the same deciding line as git check-ignore -v"

echo "▸ where CODEOWNERS differs from .gitignore (the .gitignore column confirmed by git)"
printf '%s\n' '*.log @ops' '/tools/* @tooling' '!keep.log' '/src/[ab]*.py @core' '/vendor/ @deps' '*.md @docs' > "$WORK/diff.CODEOWNERS"
DIFF_PATHS=(tools/ci/run.sh keep.log src/a1.py vendor/x/README.md tools/lint.sh x.log)
python3 "$MQ" owners --codeowners "$WORK/diff.CODEOWNERS" --compare "${DIFF_PATHS[@]}" > "$WORK/compare.txt" || fail "owners --compare"
sed 's/^/  /' "$WORK/compare.txt"
python3 "$MQ" owners --codeowners "$WORK/diff.CODEOWNERS" --compare --json "${DIFF_PATHS[@]}" > "$WORK/compare.json"
make_tree "$WORK/diff" "${DIFF_PATHS[@]}"
awk '{print $1}' "$WORK/diff.CODEOWNERS" > "$WORK/diff/.gitignore"
check_ignore "$WORK/diff" "$WORK/git.txt"
python3 - "$WORK/compare.json" "$(lines_of "$WORK/git.txt")" <<'PY' || fail "compare: $(cat "$WORK/compare.txt")"
import json, sys
found = {d["path"]: d for d in json.load(open(sys.argv[1]))}
git = json.loads(sys.argv[2])
assert sorted(found) == ["keep.log", "src/a1.py", "tools/ci/run.sh", "vendor/x/README.md"], sorted(found)
for path, d in found.items():
    assert d["gitignore"] == git[path], (path, d["gitignore"], git[path])
assert "covers only files directly inside" in found["tools/ci/run.sh"]["why"]
assert "negation" in found["keep.log"]["why"] and "character ranges" in found["src/a1.py"]["why"]
assert "stops at the directory" in found["vendor/x/README.md"]["why"]
PY

echo "▸ build the shop repository and nine branches with plain git"
SRC="$WORK/shop"
mkdir -p "$SRC"
git -C "$SRC" init -q
STAMP=1790000000
write() { mkdir -p "$(dirname "$SRC/$1")"; cat > "$SRC/$1"; }
commit() {
  STAMP=$((STAMP + 3600))
  git -C "$SRC" add -A
  GIT_AUTHOR_DATE="$STAMP +0000" GIT_COMMITTER_DATE="$STAMP +0000" git -C "$SRC" -c user.name="$1" -c user.email="$1@example.invalid" commit -q -m "$2"
}
branch() { git -C "$SRC" checkout -q -b "$1" main; }
write app/__init__.py < /dev/null
write app/prices.py <<'EOF'
def total(items):
    return sum(items)


def selftest():
    assert total([2, 3]) == 5
EOF
write app/report.py <<'EOF'
from app import prices


def summary(items):
    return f"{len(items)} items, total {prices.total(items)}"


def selftest():
    assert summary([2, 3]) == "2 items, total 5"
EOF
write app/tax.py <<'EOF'
RATE = 0.19


def selftest():
    assert 0 < RATE < 1
EOF
printf '[server]\ntimeout = 30\nworkers = 4\n' | write config/settings.ini
printf '# Guide\n\nRun the checks with `python3 check.py`.\n' | write docs/guide.md
printf '# shop\n' | write README.md
write .github/CODEOWNERS <<'EOF'
*              @acme/maintainers
/app/          @acme/backend
/config/       @acme/sre
*.md           @acme/docs
EOF
write check.py <<'EOF'
import importlib
import pkgutil
import sys

import app

failed = 0
for module in pkgutil.iter_modules(app.__path__):
    try:
        importlib.import_module(f"app.{module.name}").selftest()
    except Exception as error:
        failed += 1
        print(f"FAIL app.{module.name}: {type(error).__name__}: {error}".rstrip(": "), file=sys.stderr)
sys.exit(1 if failed else 0)
EOF
commit ana "Start the shop"
branch rename-total
write app/prices.py <<'EOF'
def sum_prices(items):
    return sum(items)


def selftest():
    assert sum_prices([2, 3]) == 5
EOF
sed 's/prices\.total/prices.sum_prices/' "$SRC/app/report.py" > "$WORK/report.py" && mv "$WORK/report.py" "$SRC/app/report.py"
commit ana "Rename prices.total to prices.sum_prices"
branch docs-guide
printf '\nEvery change lands through the merge queue.\n' >> "$SRC/docs/guide.md"
commit cy "Explain the merge queue in the guide"
branch invoice
write app/invoice.py <<'EOF'
from app import prices


def due(items):
    return round(prices.total(items) * 1.19, 2)


def selftest():
    assert due([10]) == 11.9
EOF
commit bo "Add invoices"
branch timeout-60
printf '[server]\ntimeout = 60\nworkers = 4\n' | write config/settings.ini
commit eve "Raise the timeout to 60"
branch timeout-45
printf '[server]\ntimeout = 45\nworkers = 4\n' | write config/settings.ini
commit fay "Raise the timeout to 45"
branch bad-tax
printf 'RATE = 19\n\n\ndef selftest():\n    assert 0 < RATE < 1\n' | write app/tax.py
commit ana "Store the tax rate in percent"
branch readme
printf '# shop\n\nA small shop backend used by the merge queue lesson.\n' | write README.md
commit dee "Describe the shop"
branch workers-8
printf '[server]\ntimeout = 30\nworkers = 8\n' | write config/settings.ini
commit bo "Run eight workers"
branch stale-approval
printf 'def apply(price, percent):\n    return price * (100 - percent) / 100\n\n\ndef selftest():\n    assert apply(200, 10) == 180\n' | write app/discount.py
commit cy "Add discounts"
printf 'def apply(price, percent):\n    return round(price * (100 - percent) / 100, 2)\n\n\ndef selftest():\n    assert apply(200, 10) == 180\n' | write app/discount.py
commit cy "Round discounted prices"
git -C "$SRC" checkout -q --detach main
ORIGIN="$WORK/origin.git"
git clone -q --bare "$SRC" "$ORIGIN"
START="$(git -C "$ORIGIN" rev-parse main)"
git -C "$ORIGIN" for-each-ref --format='  %(refname:short) %(objectname:short) %(subject)' refs/heads/

python3 - "$ORIGIN" "$WORK/prs.json" <<'PY'
import json, subprocess, sys
origin, out = sys.argv[1:3]
plan = {"rename-total": ("ana", "bo"), "docs-guide": ("cy", "dee"), "invoice": ("bo", "ana"), "timeout-60": ("eve", "sam"), "timeout-45": ("fay", "sam"),
        "bad-tax": ("ana", "bo"), "readme": ("dee", "cy"), "workers-8": ("bo", "ana"), "stale-approval": ("cy", "bo")}
facts = {}
for name, (author, reviewer) in plan.items():
    first = subprocess.run(["git", "-C", origin, "rev-list", "--reverse", f"main..{name}"], capture_output=True, text=True, check=True).stdout.split()[0]
    facts[name] = {"author": author, "reviews": [{"user": reviewer, "state": "APPROVED", "commit_id": first}], "checks": {"test": "success"}}
json.dump(facts, open(out, "w"), indent=2)
PY
cat > "$WORK/teams.json" <<'EOF'
{"@acme/maintainers": ["ana", "bo", "cy", "dee", "eve", "fay", "sam"], "@acme/backend": ["ana", "bo"], "@acme/sre": ["sam", "eve"], "@acme/docs": ["dee", "cy"]}
EOF

QUEUE=(rename-total docs-guide invoice timeout-60 timeout-45 bad-tax readme workers-8 stale-approval)
PASSING=(rename-total docs-guide timeout-60 readme)
fresh() { rm -rf "$WORK/q"; cp -R "$ORIGIN" "$WORK/q"; }
queue() {
  local out="$1" rc=0; shift
  python3 "$MQ" queue --repo "$WORK/q" --prs "$WORK/prs.json" --teams "$WORK/teams.json" --check "python3 check.py" --json "$@" > "$out" 2> "$out.err" || rc=$?
  [[ "$rc" -le 1 ]] || fail "mergequeue queue $*: exit $rc: $(cat "$out.err")"
  QUEUE_EXIT="$rc"
}
report() {
  python3 - "$1" "$2" "$3" "$4" <<'PY' || fail "queue report $1: $(head -c 3000 "$1")"
import json, sys
r = json.load(open(sys.argv[1]))
assert r["merged"] == sys.argv[2].split(), ("merged", r["merged"])
want_rounds, want_runs = (int(x) for x in sys.argv[3].split("/"))
assert (r["rounds"], r["runs"]) == (want_rounds, want_runs), ("rounds/runs", r["rounds"], r["runs"])
assert r["method"] == sys.argv[4], ("method", r["method"])
print(f"  {r['method']}, concurrency {r['concurrency']}, batch {r['batch']}: merged {len(r['merged'])}, ejected {len(r['ejected'])}, blocked {len(r['blocked'])}, {r['rounds']} rounds, {r['runs']} CI runs")
PY
}

echo "▸ the oracle: plain git merge of the passing branches in order, and what plain git says about the others"
ORACLE="$WORK/oracle"
git clone -q "$ORIGIN" "$ORACLE"
git -C "$ORACLE" checkout -q --detach "$START"
for b in "${PASSING[@]}"; do git -C "$ORACLE" merge -q --no-edit "origin/$b" > "$WORK/merge.txt" 2>&1 || fail "oracle merge of $b: $(cat "$WORK/merge.txt")"; done
WANT="$(git -C "$ORACLE" rev-parse 'HEAD^{tree}')"
(cd "$ORACLE" && python3 check.py) > "$WORK/oracle-check.txt" 2>&1 || fail "the oracle main fails its own check: $(cat "$WORK/oracle-check.txt")"
git -C "$ORACLE" merge -q --no-edit origin/invoice > "$WORK/merge.txt" 2>&1 || fail "invoice should merge cleanly as text: $(cat "$WORK/merge.txt")"
rc=0; (cd "$ORACLE" && python3 check.py) > "$WORK/oracle-check.txt" 2>&1 || rc=$?
[[ "$rc" == 1 ]] && grep -q "has no attribute 'total'" "$WORK/oracle-check.txt" || fail "main + invoice should fail the check (exit $rc): $(cat "$WORK/oracle-check.txt")"
echo "  invoice merges without a conflict and breaks the build: $(cat "$WORK/oracle-check.txt")"
git -C "$ORACLE" reset -q --hard HEAD~1
rc=0; git -C "$ORACLE" merge -q --no-edit origin/timeout-45 > "$WORK/merge.txt" 2>&1 || rc=$?
[[ "$rc" != 0 ]] && grep -q "CONFLICT" "$WORK/merge.txt" || fail "timeout-45 should conflict with timeout-60 in plain git: $(cat "$WORK/merge.txt")"
git -C "$ORACLE" merge --abort
git -C "$ORACLE" checkout -q --detach origin/bad-tax
rc=0; (cd "$ORACLE" && python3 check.py) > "$WORK/oracle-check.txt" 2>&1 || rc=$?
[[ "$rc" == 1 ]] || fail "bad-tax should fail its check on its own branch: $(cat "$WORK/oracle-check.txt")"
echo "  timeout-45 conflicts in plain git; bad-tax fails on its own branch; oracle tree $WANT"

echo "▸ mergequeue with the ruleset: squash, 3 candidates at once; main equals the oracle and stays linear"
fresh
queue "$WORK/run.json" --ruleset "$FIXTURES/ruleset.json" "${QUEUE[@]}"
[[ "$QUEUE_EXIT" == 1 ]] || fail "queue should exit 1 when it ejects or blocks, got $QUEUE_EXIT"
report "$WORK/run.json" "${PASSING[*]}" 3/7 squash
python3 - "$WORK/run.json" <<'PY' || fail "ejections and blocks: $(head -c 3000 "$WORK/run.json")"
import json, sys
r = json.load(open(sys.argv[1]))
assert sorted(r["ejected"]) == ["bad-tax", "invoice", "timeout-45"], r["ejected"]
assert "has no attribute 'total'" in r["ejected"]["invoice"], r["ejected"]["invoice"]
assert r["ejected"]["timeout-45"] == "conflict in config/settings.ini", r["ejected"]["timeout-45"]
assert "AssertionError" in r["ejected"]["bad-tax"], r["ejected"]["bad-tax"]
assert sorted(r["blocked"]) == ["stale-approval", "workers-8"], r["blocked"]
assert r["blocked"]["workers-8"][0].startswith("codeowners: config/settings.ini needs an approval from @acme/sre"), r["blocked"]["workers-8"]
assert r["blocked"]["stale-approval"][0].startswith("approvals: 0 of 1") and "stale" in r["blocked"]["stale-approval"][0], r["blocked"]["stale-approval"]
print("  ejected: " + "; ".join(f"{k} ({v[:60]})" for k, v in sorted(r["ejected"].items())))
print("  blocked: " + "; ".join(f"{k} ({v[0].split(':')[0]})" for k, v in sorted(r["blocked"].items())))
PY
GOT="$(git -C "$WORK/q" rev-parse 'main^{tree}')"
[[ "$GOT" == "$WANT" ]] || fail "queue main tree $GOT differs from the oracle tree $WANT"
[[ "$(git -C "$WORK/q" rev-list --merges --count main)" == 0 ]] || fail "linear history required, but main has merge commits"
[[ "$(git -C "$WORK/q" rev-list --count "$START..main")" == 4 ]] || fail "expected one squash commit per merged branch: $(git -C "$WORK/q" log --oneline "$START..main")"
git -C "$WORK/q" log --reverse --format=%s "$START..main" > "$WORK/subjects.txt"
python3 - "$WORK/subjects.txt" "${PASSING[@]}" <<'PY' || fail "squash subjects: $(cat "$WORK/subjects.txt")"
import re, sys
names = [re.search(r"\(([^)]+)\)$", line.strip()).group(1) for line in open(sys.argv[1])]
assert names == sys.argv[2:], names
PY
git -C "$WORK/q" fsck --strict --no-progress > "$WORK/fsck.txt" 2>&1 || fail "git fsck --strict: $(cat "$WORK/fsck.txt")"
[[ -z "$(git -C "$WORK/q" for-each-ref refs/mergequeue)" ]] || fail "candidate refs left behind: $(git -C "$WORK/q" for-each-ref refs/mergequeue)"
[[ "$(git -C "$WORK/q" worktree list | wc -l | tr -d ' ')" == 1 ]] || fail "worktrees left behind: $(git -C "$WORK/q" worktree list)"
git -C "$WORK/q" log --oneline "$START..main" | sed 's/^/  /'

echo "▸ the same queue serially and in batches: the same tree, different cost"
fresh
queue "$WORK/serial.json" --ruleset "$FIXTURES/ruleset.json" --concurrency 1 "${QUEUE[@]}"
report "$WORK/serial.json" "${PASSING[*]}" 6/6 squash
[[ "$(git -C "$WORK/q" rev-parse 'main^{tree}')" == "$WANT" ]] || fail "serial queue tree differs from the oracle"
fresh
queue "$WORK/batch.json" --ruleset "$FIXTURES/ruleset.json" --concurrency 1 --batch 4 "${QUEUE[@]}"
report "$WORK/batch.json" "${PASSING[*]}" 8/8 squash
[[ "$(git -C "$WORK/q" rev-parse 'main^{tree}')" == "$WANT" ]] || fail "batched queue tree differs from the oracle"
fresh
queue "$WORK/clean.json" --ruleset "$FIXTURES/ruleset.json" --concurrency 1 --batch 4 "${PASSING[@]}"
report "$WORK/clean.json" "${PASSING[*]}" 1/1 squash

echo "▸ merge and rebase methods: the ruleset blocks merge commits; without it they give the same tree"
fresh
queue "$WORK/blocked.json" --ruleset "$FIXTURES/ruleset.json" --method merge "${QUEUE[@]}"
python3 - "$WORK/blocked.json" <<'PY' || fail "--method merge under required_linear_history: $(head -c 2000 "$WORK/blocked.json")"
import json, sys
r = json.load(open(sys.argv[1]))
assert r["merged"] == [] and len(r["blocked"]) == 9, (r["merged"], sorted(r["blocked"]))
assert all(any(v.startswith("linear-history") for v in reasons) for reasons in r["blocked"].values())
PY
[[ "$(git -C "$WORK/q" rev-parse main)" == "$START" ]] || fail "main moved although every entry was blocked"
fresh
queue "$WORK/merge.json" --method merge --concurrency 2 "${PASSING[@]}" invoice
report "$WORK/merge.json" "${PASSING[*]}" 3/5 merge
[[ "$(git -C "$WORK/q" rev-parse 'main^{tree}')" == "$WANT" ]] || fail "merge method tree differs from the oracle"
[[ "$(git -C "$WORK/q" rev-list --merges --count "$START..main")" == 4 ]] || fail "merge method should create 4 merge commits"
[[ "$(git -C "$WORK/q" rev-list --first-parent --count "$START..main")" == 4 ]] || fail "first-parent history should hold one merge per branch"
fresh
queue "$WORK/rebase.json" --method rebase --concurrency 3 "${PASSING[@]}"
report "$WORK/rebase.json" "${PASSING[*]}" 2/4 rebase
[[ "$(git -C "$WORK/q" rev-parse 'main^{tree}')" == "$WANT" ]] || fail "rebase method tree differs from the oracle"
[[ "$(git -C "$WORK/q" log --format=%an "$START..main" | sort -u | tr '\n' ' ')" == "ana cy dee eve " ]] || fail "rebased commits should keep their authors: $(git -C "$WORK/q" log --format='%an %cn %s' "$START..main")"
git -C "$WORK/q" fsck --strict --no-progress > "$WORK/fsck.txt" 2>&1 || fail "git fsck --strict after rebase: $(cat "$WORK/fsck.txt")"

echo "▸ --dry-run tests everything and moves nothing; a checked-out base branch is refused"
fresh
queue "$WORK/dry.json" --ruleset "$FIXTURES/ruleset.json" --dry-run "${QUEUE[@]}"
report "$WORK/dry.json" "${PASSING[*]}" 3/7 squash
[[ "$(git -C "$WORK/q" rev-parse main)" == "$START" ]] || fail "--dry-run moved main"
git -C "$SRC" checkout -q main
rc=0; python3 "$MQ" queue --repo "$SRC" --check true readme > /dev/null 2> "$WORK/refuse.txt" || rc=$?
[[ "$rc" == 2 ]] && grep -q "is checked out" "$WORK/refuse.txt" || fail "a checked-out main should be refused with exit 2, got $rc: $(cat "$WORK/refuse.txt")"
git -C "$SRC" checkout -q --detach main

echo "▸ stacked branches: git rebase --update-refs moves the whole stack, then the queue lands it bottom first"
STACK="$WORK/stack"
mkdir -p "$STACK"
git -C "$STACK" init -q
echo base > "$STACK/base.txt"; git -C "$STACK" add -A; git -C "$STACK" commit -q -m "base"
for part in 1 2 3; do
  git -C "$STACK" checkout -q -b "part$part"
  echo "part $part" > "$STACK/part$part.txt"; git -C "$STACK" add -A; git -C "$STACK" commit -q -m "add part $part"
done
git -C "$STACK" checkout -q main
echo hotfix > "$STACK/hotfix.txt"; git -C "$STACK" add -A; git -C "$STACK" commit -q -m "hotfix on main"
OLD1="$(git -C "$STACK" rev-parse part1)"; OLD2="$(git -C "$STACK" rev-parse part2)"
git -C "$STACK" checkout -q part3
git -C "$STACK" rebase --update-refs main > "$WORK/rebase.txt" 2>&1 || fail "git rebase --update-refs: $(cat "$WORK/rebase.txt")"
grep -q "refs/heads/part1" "$WORK/rebase.txt" && grep -q "refs/heads/part2" "$WORK/rebase.txt" || fail "rebase did not report the updated refs: $(cat "$WORK/rebase.txt")"
[[ "$(git -C "$STACK" rev-parse part1)" != "$OLD1" && "$(git -C "$STACK" rev-parse part2)" != "$OLD2" ]] || fail "part1 and part2 did not move"
[[ "$(git -C "$STACK" rev-parse part1~1)" == "$(git -C "$STACK" rev-parse main)" ]] || fail "part1 is not on top of main after the rebase"
git -C "$STACK" merge-base --is-ancestor part1 part2 && git -C "$STACK" merge-base --is-ancestor part2 part3 || fail "the stack is no longer a chain"
git -C "$STACK" checkout -q --detach main
rc=0; python3 "$MQ" queue --repo "$STACK" --method squash --concurrency 3 --check "test -f base.txt && test -f hotfix.txt" --json part1 part2 part3 > "$WORK/stack.json" 2> "$WORK/stack.err" || rc=$?
[[ "$rc" == 0 ]] || fail "queueing the stack: exit $rc: $(cat "$WORK/stack.err")"
[[ "$(git -C "$STACK" log --format=%s main | head -n 3 | tr '\n' '|')" == "add part 3 (part3)|add part 2 (part2)|add part 1 (part1)|" ]] || fail "stack history: $(git -C "$STACK" log --oneline main)"
[[ "$(git -C "$STACK" ls-tree --name-only main | tr '\n' ' ')" == "base.txt hotfix.txt part1.txt part2.txt part3.txt " ]] || fail "stack tree: $(git -C "$STACK" ls-tree --name-only main)"
echo "  part1, part2 and part3 moved onto the hotfix together and landed as three squash commits"

if command -v ssh-keygen >/dev/null; then
  echo "▸ required_signatures: an SSH-signed branch is admitted, an unsigned one is blocked"
  SIG="$WORK/signed"
  mkdir -p "$SIG"
  git -C "$SIG" init -q
  ssh-keygen -q -t ed25519 -N "" -C smoke -f "$WORK/signing_key"
  echo "smoke@example.invalid $(cat "$WORK/signing_key.pub")" > "$WORK/allowed_signers"
  git -C "$SIG" config gpg.ssh.allowedSignersFile "$WORK/allowed_signers"
  echo base > "$SIG/base.txt"; git -C "$SIG" add -A; git -C "$SIG" commit -q -m "base"
  git -C "$SIG" checkout -q -b signed
  echo signed > "$SIG/signed.txt"; git -C "$SIG" add -A
  git -C "$SIG" -c gpg.format=ssh -c user.signingkey="$WORK/signing_key" commit -q -S -m "signed change"
  git -C "$SIG" checkout -q -b unsigned main
  echo unsigned > "$SIG/unsigned.txt"; git -C "$SIG" add -A; git -C "$SIG" commit -q -m "unsigned change"
  git -C "$SIG" checkout -q --detach main
  git -C "$SIG" verify-commit signed > "$WORK/verify.txt" 2>&1 || fail "git verify-commit signed: $(cat "$WORK/verify.txt")"
  [[ "$(git -C "$SIG" log -1 --format=%G? signed)" == G && "$(git -C "$SIG" log -1 --format=%G? unsigned)" == N ]] || fail "unexpected %G?: $(git -C "$SIG" log --format='%G? %s' signed unsigned)"
  cat > "$WORK/signed-ruleset.json" <<'EOF'
{"name": "signed-main", "enforcement": "active", "rules": [{"type": "required_signatures"}, {"type": "merge_queue", "parameters": {"merge_method": "MERGE", "max_entries_to_build": 2}}]}
EOF
  rc=0; python3 "$MQ" queue --repo "$SIG" --ruleset "$WORK/signed-ruleset.json" --json signed unsigned > "$WORK/signed.json" 2> "$WORK/signed.err" || rc=$?
  [[ "$rc" == 1 ]] || fail "signed queue: exit $rc: $(cat "$WORK/signed.err")"
  python3 - "$WORK/signed.json" <<'PY' || fail "signatures: $(cat "$WORK/signed.json")"
import json, sys
r = json.load(open(sys.argv[1]))
assert r["merged"] == ["signed"], r["merged"]
assert list(r["blocked"]) == ["unsigned"] and r["blocked"]["unsigned"][0].startswith("signatures: unsigned or unverified commits"), r["blocked"]
PY
  git -C "$SIG" merge-base --is-ancestor signed main || fail "the signed commit is not on main"
  echo "  git verify-commit accepts the signed branch; mergequeue landed it and blocked the unsigned one"
else
  echo "  (ssh-keygen not installed; the required_signatures check is skipped)"
fi

echo "▸ simulate: conflicts grow with branch lifetime and team size; a saturated serial queue needs concurrency"
python3 "$MQ" simulate --developers 8 --branch-hours 4,40 --json > "$WORK/sim-lifetime.json"
python3 "$MQ" simulate --developers 4,30 --branch-hours 16 --concurrency 4 --json > "$WORK/sim-team.json"
python3 "$MQ" simulate --developers 30 --branch-hours 4 --concurrency 1 --json > "$WORK/sim-serial.json"
python3 "$MQ" simulate --developers 30 --branch-hours 4 --concurrency 4 --json > "$WORK/sim-parallel.json"
python3 - "$WORK" <<'PY' || fail "simulator trends"
import json, os, sys
load = lambda name: json.load(open(os.path.join(sys.argv[1], name)))
short, long = load("sim-lifetime.json")
small, large = load("sim-team.json")
(serial,), (parallel,) = load("sim-serial.json"), load("sim-parallel.json")
assert long["conflict_rate"] > short["conflict_rate"] + 0.25, (short, long)
assert large["conflict_rate"] > small["conflict_rate"] + 0.25, (small, large)
assert serial["load"] > 1 > parallel["load"], (serial["load"], parallel["load"])
assert serial["wait_p50_minutes"] > 20 * parallel["wait_p50_minutes"], (serial, parallel)
print(f"  8 devs: {short['conflict_rate']:.0%} of 4 h branches conflict, {long['conflict_rate']:.0%} of 40 h branches")
print(f"  16 h branches: {small['conflict_rate']:.0%} with 4 devs, {large['conflict_rate']:.0%} with 30")
print(f"  30 devs on 4 h branches: queue p50 {serial['wait_p50_minutes']:.0f} min serially (load {serial['load']}), {parallel['wait_p50_minutes']:.0f} min with 4 candidates")
PY

echo "SMOKE OK: git/merge-queue"
