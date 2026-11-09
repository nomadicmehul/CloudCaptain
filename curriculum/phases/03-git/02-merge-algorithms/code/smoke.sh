#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
LESSON="$PWD"
MM="$LESSON/code/main.py"
FIXTURES="$LESSON/code/fixtures"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
command -v git >/dev/null || fail "git is not installed"
command -v patch >/dev/null || fail "patch is not installed"
command -v python3 >/dev/null || fail "python3 is not installed"

export GIT_CONFIG_GLOBAL="$WORK/gitconfig" GIT_CONFIG_NOSYSTEM=1
: > "$GIT_CONFIG_GLOBAL"
git config --global init.defaultBranch main
git config --global commit.gpgsign false
git config --global gc.auto 0
git config --global advice.detachedHead false
git config --global merge.conflictStyle merge
export GIT_AUTHOR_NAME="Ada Lovelace" GIT_AUTHOR_EMAIL=ada@example.invalid GIT_AUTHOR_DATE="@1767600000 +0100"
export GIT_COMMITTER_NAME="Ada Lovelace" GIT_COMMITTER_EMAIL=ada@example.invalid GIT_COMMITTER_DATE="@1767600000 +0100"
patch --version > "$WORK/patch-version.txt" 2>&1 || true
echo "▸ $(git version), $(head -1 "$WORK/patch-version.txt"), $(python3 --version)"

mm() { python3 "$MM" "$@"; }

cat > "$WORK/check.py" <<'PY'
import json
import os
import random
import re
import shutil
import sys
from collections import Counter


def die(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def write(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("".join(lines))


def read(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def lines_of(text):
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def mutate(rng, lines, alphabet, edits):
    out = list(lines)
    for _ in range(edits):
        kind, at = rng.random(), rng.randint(0, len(out))
        if kind < 0.35 and out:
            at = min(at, len(out) - 1)
            del out[at:at + rng.randint(1, 3)]
        elif kind < 0.7:
            out[at:at] = [rng.choice(alphabet) for _ in range(rng.randint(1, 3))]
        elif out:
            out[min(at, len(out) - 1)] = rng.choice(alphabet)
    return out


def function(name, body):
    return [f"int {name}(int n)\n", "{\n"] + [f"    {line};\n" for line in body] + ["    return n;\n", "}\n", "\n"]


def corpus(folder, count, seed):
    rng = random.Random(int(seed))
    letters = [f"{c}\n" for c in "abcdef"] + ["}\n", "\n"]
    words = [f"line {n}\n" for n in range(40)]
    made = 0
    for n in range(int(count)):
        family = n % 5
        if family == 0:
            old = [rng.choice(letters) for _ in range(rng.randint(0, 30))]
            new = mutate(rng, old, letters, rng.randint(1, 6))
        elif family == 1:
            blocks = [function(f"f{k}", [f"n = n * {k}", f"n += {k + 1}"]) for k in range(rng.randint(3, 6))]
            moved = list(blocks)
            moved.insert(rng.randrange(len(moved)), moved.pop(rng.randrange(len(moved))))
            old, new = sum(blocks, []), mutate(rng, sum(moved, []), ["    n -= 1;\n", "}\n", "{\n", "\n"], rng.randint(0, 3))
        elif family == 2:
            old = [rng.choice(words) for _ in range(rng.randint(50, 200))]
            new = mutate(rng, old, words, rng.randint(1, 8))
        elif family == 3:
            old = [rng.choice(letters) for _ in range(rng.randint(1, 12))]
            new = mutate(rng, old, letters, rng.randint(1, 4))
            for side in (old, new):
                if side and side[-1] != "\n" and rng.random() < 0.6:
                    side[-1] = side[-1].rstrip("\n")
        else:
            old = [] if n % 2 else [rng.choice(words) for _ in range(rng.randint(1, 10))]
            new = [rng.choice(words) for _ in range(rng.randint(1, 10))] if n % 2 else []
        write(os.path.join(folder, f"{n:03d}", "old"), old)
        write(os.path.join(folder, f"{n:03d}", "new"), new)
        made += 1
    print(made)


def hunk_body(path):
    removed, added, at_old, at_new, last = [], [], 0, 0, None
    for line in lines_of(read(path)):
        header = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if header:
            at_old, at_new = int(header[1]), int(header[3])
            continue
        if line.startswith(("diff ", "index ", "--- ", "+++ ", "new file", "deleted file")):
            continue
        if line.startswith("\\"):
            if last is not None:
                last[-1][1] = last[-1][1].rstrip("\n")
            continue
        if line.startswith("-"):
            removed.append([at_old, line[1:]])
            at_old, last = at_old + 1, removed
        elif line.startswith("+"):
            added.append([at_new, line[1:]])
            at_new, last = at_new + 1, added
        elif line.startswith(" "):
            at_old, at_new, last = at_old + 1, at_new + 1, None
    return [tuple(r) for r in removed], [tuple(a) for a in added]


def mine(path):
    data = json.loads(read(path))
    removed = [(c["old_start"] + k, None) for c in data["changes"] for k in range(c["old_count"])]
    added = [(c["new_start"] + k, None) for c in data["changes"] for k in range(c["new_count"])]
    return [(p, t) for (p, _), t in zip(removed, data["removed"])], [(p, t) for (p, _), t in zip(added, data["added"])]


def diffcheck(git_default, git_plain, *jsons):
    removed, added = hunk_body(git_default)
    exact = hunk_body(git_plain)
    names = ["exact", "exact with --no-rediff (Git before 2.54)"]
    seen = []
    for name, path in zip(names, jsons):
        ours = mine(path)
        same_lines = Counter(t for _, t in removed) == Counter(t for _, t in ours[0]) and Counter(t for _, t in added) == Counter(t for _, t in ours[1])
        if same_lines and ours == exact:
            print(name)
            return
        seen.append(f"{name}: removed {ours[0]} added {ours[1]}")
    die(f"git diff (indent heuristic on) removed {removed} added {added}; with diff.indentHeuristic=false removed {exact[0]} added {exact[1]}; minimerge " + "; ".join(seen))


def cases(folder, seed, fixtures):
    table = {
        "disjoint": ("1\n2\n3\n4\n5\n6\n", "1\nTWO\n3\n4\n5\n6\n", "1\n2\n3\n4\n5\nSIX\n"),
        "same-change": ("a\nb\nc\n", "a\nB\nc\n", "a\nB\nc\n"),
        "overlap": ("a\nb\nc\nd\n", "a\nB1\nC1\nd\n", "a\nb\nC2\nd\n"),
        "delete-vs-modify": ("a\nb\nc\n", "a\nc\n", "a\nB\nc\n"),
        "both-append": ("a\n", "a\nfrom ours\n", "a\nfrom theirs\n"),
        "no-final-newline": ("a\nb\n", "a\nO", "a\nT"),
        "common-inside": ("a\nX\nb\n", "a\n1\n2\nY\n3\nb\n", "a\n1\n2\nZ\n3\nb\n"),
        "punctuation-gap": ("1\n2\n}\n}\n\n}\n8\n9\n", "1\nA\n}\n}\n\n}\nB\n9\n", "1\nC\n}\n}\n\n}\nD\n9\n"),
        "one-side-only": ("a\nb\n", "a\nb\n", "a\nb\nc\n"),
    }
    for name, (base, ours, theirs) in table.items():
        for role, text in (("base", base), ("ours", ours), ("theirs", theirs)):
            write(os.path.join(folder, name, role), [text])
    for name, ext in (("config", "yaml"), ("json", "json")):
        for role in ("base", "ours", "theirs"):
            os.makedirs(os.path.join(folder, name), exist_ok=True)
            shutil.copyfile(os.path.join(fixtures, name, f"{role}.{ext}"), os.path.join(folder, name, role))
    rng = random.Random(int(seed))
    letters = [f"{c}\n" for c in "abcdefgh"] + ["}\n", "\n"]
    for n in range(12):
        base = [rng.choice(letters) for _ in range(rng.randint(4, 25))]
        write(os.path.join(folder, f"random-{n:02d}", "base"), base)
        write(os.path.join(folder, f"random-{n:02d}", "ours"), mutate(rng, base, letters, rng.randint(1, 4)))
        write(os.path.join(folder, f"random-{n:02d}", "theirs"), mutate(rng, base, letters, rng.randint(1, 4)))
    print(len(os.listdir(folder)))


def mergecheck(git_out, git_code, mm_json):
    data = json.loads(read(mm_json))
    theirs = read(git_out)
    if data["result"] != theirs:
        die(f"merged text differs.\n--- git merge-file\n{theirs}--- minimerge\n{data['result']}")
    if data["conflicts"] != int(git_code):
        die(f"git merge-file reported {git_code} conflict(s), minimerge {data['conflicts']}")
    print(data["conflicts"])


def sameset(left, right):
    a, b = set(read(left).split()), set(read(right).split())
    if a != b:
        die(f"git: {sorted(a)} minimerge: {sorted(b)}")
    print(len(a))


def renames(git_file, mm_json):
    theirs = set()
    for line in read(git_file).splitlines():
        parts = line.split("\t")
        if parts[0].startswith("R"):
            theirs.add((parts[1].split("/", 1)[1], parts[2].split("/", 1)[1], int(parts[0][1:])))
    data = json.loads(read(mm_json))
    mine_set = {(r["source"], r["target"], r["similarity"]) for r in data["renames"]}
    if theirs != mine_set:
        die(f"renames differ: git {sorted(theirs)} minimerge {sorted(mine_set)}")
    for source, target, score in sorted(mine_set):
        print(f"  R{score:03d} {source} -> {target}")


def dupkeys(path):
    keys = json.loads(read(path), object_pairs_hook=lambda items: [k for k, _ in items])
    print(",".join(sorted(k for k, n in Counter(keys).items() if n > 1)))


def count(path, key):
    print(len(json.loads(read(path))[key]))


COMMANDS = {"corpus": corpus, "diffcheck": diffcheck, "cases": cases, "mergecheck": mergecheck, "sameset": sameset, "renames": renames, "dupkeys": dupkeys, "count": count}
COMMANDS[sys.argv[1]](*sys.argv[2:])
PY
check() { python3 "$WORK/check.py" "$@"; }

echo "▸ diff: a generated corpus, three algorithms; minimerge's patch applies with patch(1) and changes the same lines as git diff --no-index"
CORPUS="$WORK/corpus"
check corpus "$CORPUS" 30 20261109 > "$WORK/count.txt" || fail "corpus generation"
mkdir -p "$CORPUS/fixture" "$CORPUS/shifted" && cp "$FIXTURES/patience/old.c" "$CORPUS/fixture/old" && cp "$FIXTURES/patience/new.c" "$CORPUS/fixture/new"
printf 'b\n}\n' > "$CORPUS/shifted/old" && printf '}\nb\na\n}\n' > "$CORPUS/shifted/new"
: > "$WORK/diff-summary.txt"
for pair in "$CORPUS"/*/; do
  pair="${pair%/}"
  for alg in myers patience histogram; do
    CODE=0
    mm diff --algorithm "$alg" "$pair/old" "$pair/new" > "$WORK/mm.patch" 2> "$WORK/mm.err" || CODE=$?
    [[ "$CODE" == "0" || "$CODE" == "1" ]] || fail "minimerge diff $alg $pair exit $CODE: $(cat "$WORK/mm.err")"
    if [[ "$CODE" == "1" ]]; then
      rm -f "$WORK/applied"
      patch -s -o "$WORK/applied" "$pair/old" < "$WORK/mm.patch" > "$WORK/patch.out" 2>&1 || fail "patch rejected minimerge's $alg diff of $pair: $(cat "$WORK/patch.out") patch: $(cat "$WORK/mm.patch")"
      cmp -s "$WORK/applied" "$pair/new" || fail "patch applied minimerge's $alg diff of $pair but the result differs from new"
    else
      cmp -s "$pair/old" "$pair/new" || fail "minimerge diff said $pair is unchanged"
    fi
    CODE=0
    git diff --no-index --no-color --diff-algorithm="$alg" "$pair/old" "$pair/new" > "$WORK/git.diff" || CODE=$?
    [[ "$CODE" -le 1 ]] || fail "git diff --no-index failed with exit $CODE on $pair"
    git -c diff.indentHeuristic=false diff --no-index --no-color --diff-algorithm="$alg" "$pair/old" "$pair/new" > "$WORK/git-plain.diff" || true
    mm diff --json --algorithm "$alg" "$pair/old" "$pair/new" > "$WORK/mm.json" || true
    JSONS=("$WORK/mm.json")
    if [[ "$alg" == "histogram" ]]; then
      mm diff --json --no-rediff --algorithm "$alg" "$pair/old" "$pair/new" > "$WORK/mm-norediff.json" || true
      JSONS+=("$WORK/mm-norediff.json")
    fi
    check diffcheck "$WORK/git.diff" "$WORK/git-plain.diff" "${JSONS[@]}" > "$WORK/verdict.txt" || fail "$alg diff of $(basename "$pair")"
    echo "$alg $(cat "$WORK/verdict.txt")" >> "$WORK/diff-summary.txt"
  done
done
sort "$WORK/diff-summary.txt" | uniq -c | sed 's/^/  /'

echo "▸ the patience fixture: myers interleaves the two functions, patience and histogram add and remove whole functions"
mm diff --json "$FIXTURES/patience/old.c" "$FIXTURES/patience/new.c" > "$WORK/myers.json" || true
mm diff --json --algorithm patience "$FIXTURES/patience/old.c" "$FIXTURES/patience/new.c" > "$WORK/patience.json" || true
grep -q 'int frobnitz' "$WORK/myers.json" || fail "myers should remove and re-add the frobnitz header: $(cat "$WORK/myers.json")"
if grep -q 'int frobnitz' "$WORK/patience.json"; then fail "patience should keep the frobnitz header: $(cat "$WORK/patience.json")"; fi
echo "  the same 11 removed and 10 added lines, as $(check count "$WORK/myers.json" changes) separate changes with myers and $(check count "$WORK/patience.json" changes) with patience"

echo "▸ merge-base: histories built with git, including criss-cross merges and an unrelated root; minimerge equals git merge-base --all"
R="$WORK/history"
git init -q "$R"
seq 1 9 > "$R/f"
git -C "$R" add f && git -C "$R" commit -q -m A
git -C "$R" branch x && git -C "$R" branch y
git -C "$R" checkout -q x && sed 's/^2$/2x/' "$R/f" > "$R/f.new" && mv "$R/f.new" "$R/f" && git -C "$R" commit -q -am X1
git -C "$R" checkout -q y && sed 's/^2$/2y/' "$R/f" > "$R/f.new" && mv "$R/f.new" "$R/f" && git -C "$R" commit -q -am Y1
git -C "$R" tag X1 x && git -C "$R" tag Y1 y
git -C "$R" checkout -q x && { git -C "$R" merge -q Y1 -m X2 > /dev/null 2>&1 || true; }
printf '1\n2xy\n3\n4\n5\n6\n7\n8\n9\n' > "$R/f" && git -C "$R" commit -q -am X2
git -C "$R" checkout -q y && { git -C "$R" merge -q X1 -m Y2 > /dev/null 2>&1 || true; }
printf '1\n2yx\n3\n4\n5\n6\n7\n8\n9\n' > "$R/f" && git -C "$R" commit -q -am Y2
git -C "$R" checkout -q x && sed 's/^8$/8x/' "$R/f" > "$R/f.new" && mv "$R/f.new" "$R/f" && git -C "$R" commit -q -am X3
git -C "$R" checkout -q y && sed 's/^5$/5y/' "$R/f" > "$R/f.new" && mv "$R/f.new" "$R/f" && git -C "$R" commit -q -am Y3
git -C "$R" checkout -q -b topic X1 && printf 'topic\n' > "$R/t" && git -C "$R" add t && git -C "$R" commit -q -m T1
git -C "$R" checkout -q --orphan unrelated && git -C "$R" rm -q -rf . && printf 'other\n' > "$R/o" && git -C "$R" add o && git -C "$R" commit -q -m U1
git -C "$R" checkout -q x
TREE="$(git -C "$R" hash-object -t tree -w /dev/null)"
python3 - "$TREE" > "$WORK/dag.sh" <<'PY'
import random, sys
rng = random.Random(42)
print("set -e")
for n in range(30):
    parents = rng.sample(range(n), min(n, rng.choice([1, 1, 2, 2, 3]))) if n else []
    flags = " ".join(f'-p "$C{p}"' for p in parents)
    print(f'C{n}=$(git -C "$R" commit-tree {sys.argv[1]} {flags} -m d{n})')
    print(f'git -C "$R" update-ref refs/dag/c{n} "$C{n}"')
pairs = [(rng.randrange(30), rng.randrange(30)) for _ in range(25)]
print("PAIRS=(" + " ".join(f"refs/dag/c{a}:refs/dag/c{b}" for a, b in pairs) + ")")
PY
source "$WORK/dag.sh"
PAIRS_TO_CHECK=(x:y x~1:y~1 x~2:y X1:Y1 x:X1 topic:y topic:x x:x "${PAIRS[@]}")
MULTI=0
for pair in "${PAIRS_TO_CHECK[@]}"; do
  one="${pair%%:*}" two="${pair#*:}"
  git -C "$R" merge-base --all "$one" "$two" > "$WORK/git-bases.txt" || fail "git merge-base --all $one $two"
  mm merge-base --repo "$R" "$one" "$two" > "$WORK/mm-bases.txt" 2> "$WORK/mm-bases.err" || fail "minimerge merge-base $one $two: $(cat "$WORK/mm-bases.err")"
  N="$(check sameset "$WORK/git-bases.txt" "$WORK/mm-bases.txt")" || fail "merge bases of $one and $two"
  if [[ "$N" -gt 1 ]]; then MULTI=$((MULTI + 1)); fi
done
[[ "$(wc -l < "$WORK/git-bases.txt" | tr -d ' ')" -ge 1 ]] || fail "the last pair should have a base"
git -C "$R" merge-base --all x y > "$WORK/xy.txt"
[[ "$(wc -l < "$WORK/xy.txt" | tr -d ' ')" == "2" ]] || fail "x and y should be a criss-cross with two merge bases: $(cat "$WORK/xy.txt")"
echo "  ${#PAIRS_TO_CHECK[@]} pairs agree, $MULTI of them with more than one merge base; x and y: $(git -C "$R" name-rev --name-only $(cat "$WORK/xy.txt") | tr '\n' ' ')"
CODE=0
git -C "$R" merge-base --all x unrelated > /dev/null || CODE=$?
[[ "$CODE" == "1" ]] || fail "git merge-base should exit 1 for unrelated histories, got $CODE"
CODE=0
mm merge-base --repo "$R" x unrelated > "$WORK/none.txt" 2> "$WORK/none.err" || CODE=$?
[[ "$CODE" == "1" && ! -s "$WORK/none.txt" ]] || fail "minimerge merge-base for unrelated histories: exit $CODE, $(cat "$WORK/none.txt" "$WORK/none.err")"
echo "  unrelated root: both exit 1 with no output ($(cat "$WORK/none.err"))"

echo "▸ merge-file: a table of three-way cases in merge, diff3 and zdiff3 style; minimerge equals git merge-file byte for byte"
CASES="$WORK/cases"
check cases "$CASES" 7 "$FIXTURES" > /dev/null || fail "case generation"
ALGS=(myers)
if git merge-file -p --diff-algorithm=histogram "$CASES/disjoint/ours" "$CASES/disjoint/base" "$CASES/disjoint/theirs" > /dev/null 2>&1; then
  ALGS=(myers patience histogram)
else
  echo "  (this git merge-file has no --diff-algorithm; myers only)"
fi
TOTAL=0 CONFLICTED=0 NOREDIFF=0
for case in "$CASES"/*/; do
  case="${case%/}"
  for style in merge diff3 zdiff3; do
    for alg in "${ALGS[@]}"; do
      FLAGS=()
      [[ "$style" == "merge" ]] || FLAGS+=("--$style")
      [[ "${#ALGS[@]}" == "1" ]] || FLAGS+=("--diff-algorithm=$alg")
      CODE=0
      git merge-file -p -L ours -L base -L theirs "${FLAGS[@]}" "$case/ours" "$case/base" "$case/theirs" > "$WORK/git-merge.txt" || CODE=$?
      [[ "$CODE" -lt 128 ]] || fail "git merge-file failed on $case"
      mm merge-file --json --style "$style" --algorithm "$alg" -L ours -L base -L theirs "$case/ours" "$case/base" "$case/theirs" > "$WORK/mm-merge.json" 2> /dev/null || true
      if ! N="$(check mergecheck "$WORK/git-merge.txt" "$CODE" "$WORK/mm-merge.json" 2> "$WORK/mergecheck.err")"; then
        [[ "$alg" == "histogram" ]] || fail "$(basename "$case") with --$style --diff-algorithm=$alg: $(cat "$WORK/mergecheck.err")"
        mm merge-file --json --no-rediff --style "$style" --algorithm "$alg" -L ours -L base -L theirs "$case/ours" "$case/base" "$case/theirs" > "$WORK/mm-merge.json" 2> /dev/null || true
        N="$(check mergecheck "$WORK/git-merge.txt" "$CODE" "$WORK/mm-merge.json")" || fail "$(basename "$case") with --$style --diff-algorithm=$alg, with and without --no-rediff"
        NOREDIFF=$((NOREDIFF + 1))
      fi
      TOTAL=$((TOTAL + 1))
      [[ "$N" == "0" ]] || CONFLICTED=$((CONFLICTED + 1))
    done
  done
done
echo "  $TOTAL merges identical ($CONFLICTED with conflicts) across $(ls "$CASES" | wc -l | tr -d ' ') cases, styles merge/diff3/zdiff3, algorithms ${ALGS[*]}; $NOREDIFF histogram merges matched only with --no-rediff"
mm merge-file --json -L ours -L base -L theirs "$CASES/common-inside/ours" "$CASES/common-inside/base" "$CASES/common-inside/theirs" > "$WORK/ci.json" 2> /dev/null || true
grep -q '"mode": "conflict"' "$WORK/ci.json" || fail "common-inside should conflict: $(cat "$WORK/ci.json")"

echo "▸ line merges do not understand JSON: both tools merge the JSON fixture cleanly into a file with a duplicate key"
CODE=0
git merge-file -p "$CASES/json/ours" "$CASES/json/base" "$CASES/json/theirs" > "$WORK/merged.json" || CODE=$?
[[ "$CODE" == "0" ]] || fail "git merge-file should merge the JSON fixture without conflicts, exit $CODE"
DUP="$(check dupkeys "$WORK/merged.json")" || fail "merged JSON should still parse: $(cat "$WORK/merged.json")"
[[ "$DUP" == "retries" ]] || fail "expected a duplicate key retries, got '$DUP': $(cat "$WORK/merged.json")"
mm merge-file "$CASES/json/ours" "$CASES/json/base" "$CASES/json/theirs" > "$WORK/mm-merged.json" || fail "minimerge should merge the JSON fixture cleanly"
cmp -s "$WORK/merged.json" "$WORK/mm-merged.json" || fail "minimerge and git merged the JSON differently"
echo "  exit 0 from both, duplicate key: $DUP"

echo "▸ criss-cross merge: git merge -s ort and minimerge merge-recursive build the same virtual base and the same result"
git -C "$R" checkout -q x
CODE=0
git -C "$R" merge -s ort --no-edit y > "$WORK/ort.txt" 2>&1 || CODE=$?
[[ "$CODE" == "1" ]] || fail "git merge x y should stop with a conflict: exit $CODE $(cat "$WORK/ort.txt")"
cp "$R/f" "$WORK/git-f.txt"
git -C "$R" merge --abort
CODE=0
mm merge-recursive --repo "$R" HEAD y f > "$WORK/mm-f.txt" 2> "$WORK/mm-steps.txt" || CODE=$?
[[ "$CODE" == "1" ]] || fail "minimerge merge-recursive should report a conflict: exit $CODE $(cat "$WORK/mm-steps.txt")"
cmp -s "$WORK/git-f.txt" "$WORK/mm-f.txt" || fail "criss-cross merge differs: git $(cat "$WORK/git-f.txt") minimerge $(cat "$WORK/mm-f.txt")"
grep -q "virtual" "$WORK/mm-steps.txt" || fail "minimerge should report a virtual merge base: $(cat "$WORK/mm-steps.txt")"
sed 's/^/  /' "$WORK/mm-steps.txt"

V="$WORK/virtual"
git init -q "$V"
seq 1 9 > "$V/f" && git -C "$V" add f && git -C "$V" commit -q -m A
git -C "$V" branch b
sed 's/^2$/2x/' "$V/f" > "$V/f.new" && mv "$V/f.new" "$V/f" && git -C "$V" commit -q -am X1 && git -C "$V" tag X1
git -C "$V" checkout -q b && sed 's/^7$/7y/' "$V/f" > "$V/f.new" && mv "$V/f.new" "$V/f" && git -C "$V" commit -q -am Y1 && git -C "$V" tag Y1
git -C "$V" merge -q --no-edit X1 > /dev/null
git -C "$V" checkout -q main && git -C "$V" merge -q --no-edit Y1 > /dev/null
sed 's/^2x$/2xx/' "$V/f" > "$V/f.new" && mv "$V/f.new" "$V/f" && git -C "$V" commit -q -am X3
git -C "$V" checkout -q b && sed 's/^7y$/7yy/' "$V/f" > "$V/f.new" && mv "$V/f.new" "$V/f" && git -C "$V" commit -q -am Y3
git -C "$V" checkout -q main
[[ "$(git -C "$V" merge-base --all main b | wc -l | tr -d ' ')" == "2" ]] || fail "the second history should be a criss-cross"
git -C "$V" show X1:f > "$WORK/v-base.txt" && git -C "$V" show main:f > "$WORK/v-ours.txt" && git -C "$V" show b:f > "$WORK/v-theirs.txt"
CODE=0
mm merge-file "$WORK/v-ours.txt" "$WORK/v-base.txt" "$WORK/v-theirs.txt" > /dev/null 2>&1 || CODE=$?
[[ "$CODE" == "1" ]] || fail "with only one merge base (X1) the merge should conflict on line 7, exit $CODE"
mm merge-recursive --repo "$V" main b f > "$WORK/v-mm.txt" 2> "$WORK/v-steps.txt" || fail "with a virtual base the merge should be clean: $(cat "$WORK/v-steps.txt")"
git -C "$V" merge -q -s ort --no-edit b > "$WORK/v-ort.txt" 2>&1 || fail "git merge -s ort should merge cleanly: $(cat "$WORK/v-ort.txt")"
cmp -s "$V/f" "$WORK/v-mm.txt" || fail "virtual-base merge differs: git $(cat "$V/f") minimerge $(cat "$WORK/v-mm.txt")"
echo "  one base (X1) alone: conflict on line 7; virtual base of X1 and Y1: clean in both, result $(tr '\n' ' ' < "$V/f")"

echo "▸ renames: minimerge pairs deleted and added files like git diff -M, and git merge -s ort applies an edit across a rename"
N2="$WORK/rn"
mkdir -p "$N2/old/src" "$N2/old/docs" "$N2/new/lib" "$N2/new/handbook"
python3 - "$N2" <<'PY'
import os, sys
root = sys.argv[1]
body = "".join(f"def step_{n}(value):\n    return value + {n}\n\n" for n in range(25))
def put(rel, text):
    with open(os.path.join(root, rel), "w") as handle:
        handle.write(text)
put("old/src/util.py", body)
put("new/lib/util.py", body.replace("value + 3\n", "value + 33\n").replace("value + 17\n", "value - 17\n"))
put("old/docs/guide.md", "".join(f"Step {n}: do the thing number {n}.\n" for n in range(30)))
put("new/handbook/guide.md", "".join(f"Step {n}: do the thing number {n}.\n" for n in range(30)))
put("old/src/gone.py", "print('gone')\n" * 3)
put("new/lib/fresh.py", "".join(f"value_{n} = {n * n}\n" for n in range(40)))
put("old/src/half.py", "".join(f"line {n}\n" for n in range(20)))
put("new/lib/half.py", "".join(f"line {n}\n" for n in range(10)) + "".join(f"other {n}\n" for n in range(10, 30)))
PY
(cd "$N2" && git diff --no-index -M50% --name-status old new > "$WORK/git-renames.txt") || true
mm renames --json "$N2/old" "$N2/new" > "$WORK/mm-renames.json" || fail "minimerge renames: $(cat "$WORK/mm-renames.json")"
check renames "$WORK/git-renames.txt" "$WORK/mm-renames.json" || fail "rename detection"

M="$WORK/rename-merge"
git init -q "$M"
cp "$N2/old/src/util.py" "$M/util.py" && git -C "$M" add util.py && git -C "$M" commit -q -m base
git -C "$M" checkout -q -b move && mkdir "$M/lib" && git -C "$M" mv util.py lib/util.py
sed 's/value + 3$/value + 33/' "$M/lib/util.py" > "$M/u.new" && mv "$M/u.new" "$M/lib/util.py" && git -C "$M" commit -q -am "Move util.py to lib/ and fix step 3"
git -C "$M" checkout -q main && sed 's/value + 20$/value * 20/' "$M/util.py" > "$M/u.new" && mv "$M/u.new" "$M/util.py" && git -C "$M" commit -q -am "Fix step 20"
git -C "$M" diff -M --name-status main~1 move > "$WORK/move-status.txt"
grep -q '^R[0-9]*'$'\t''util.py'$'\t''lib/util.py$' "$WORK/move-status.txt" || fail "git should see the move as a rename: $(cat "$WORK/move-status.txt")"
git -C "$M" merge -q -s ort --no-edit move > "$WORK/rename-merge.txt" 2>&1 || fail "git merge -s ort should follow the rename: $(cat "$WORK/rename-merge.txt")"
[[ ! -e "$M/util.py" ]] || fail "util.py should be gone after the merge"
git -C "$M" show "$(git -C "$M" merge-base HEAD^1 HEAD^2)":util.py > "$WORK/r-base.txt" && git -C "$M" show HEAD^1:util.py > "$WORK/r-ours.txt" && git -C "$M" show move:lib/util.py > "$WORK/r-theirs.txt"
mm merge-file "$WORK/r-ours.txt" "$WORK/r-base.txt" "$WORK/r-theirs.txt" > "$WORK/r-mm.txt" || fail "minimerge should merge the renamed file cleanly"
cmp -s "$M/lib/util.py" "$WORK/r-mm.txt" || fail "merge across the rename differs from git"
grep -q 'value \* 20' "$M/lib/util.py" && grep -q 'value + 33' "$M/lib/util.py" || fail "both fixes should be in lib/util.py: $(cat "$M/lib/util.py")"
echo "  $(cut -f1 "$WORK/move-status.txt" | head -1) util.py -> lib/util.py; ort put main's fix into lib/util.py, same bytes as minimerge merge-file"

echo "▸ Use It: git rerere replays a recorded conflict resolution"
Q="$WORK/rerere"
git init -q "$Q"
git -C "$Q" config rerere.enabled true
printf 'timeout: 30\n' > "$Q/app.yaml" && git -C "$Q" add app.yaml && git -C "$Q" commit -q -m base
git -C "$Q" checkout -q -b feature && printf 'timeout: 60\n' > "$Q/app.yaml" && git -C "$Q" commit -q -am feature
git -C "$Q" checkout -q main && printf 'timeout: 45\n' > "$Q/app.yaml" && git -C "$Q" commit -q -am main
if git -C "$Q" merge --no-edit feature > "$WORK/rerere1.txt" 2>&1; then fail "the first merge should conflict"; fi
grep -q "Recorded preimage for 'app.yaml'" "$WORK/rerere1.txt" || fail "rerere should record the preimage: $(cat "$WORK/rerere1.txt")"
printf 'timeout: 60\n' > "$Q/app.yaml" && git -C "$Q" add app.yaml && git -C "$Q" commit -q --no-edit > "$WORK/rerere2.txt" 2>&1
grep -q "Recorded resolution for 'app.yaml'" "$WORK/rerere2.txt" || fail "rerere should record the resolution: $(cat "$WORK/rerere2.txt")"
git -C "$Q" reset -q --hard HEAD~1
if git -C "$Q" merge --no-edit feature > "$WORK/rerere3.txt" 2>&1; then fail "the second merge should still stop for review"; fi
grep -q "Resolved 'app.yaml' using previous resolution" "$WORK/rerere3.txt" || fail "rerere should replay the resolution: $(cat "$WORK/rerere3.txt")"
[[ "$(cat "$Q/app.yaml")" == "timeout: 60" ]] || fail "rerere should have written the recorded resolution: $(cat "$Q/app.yaml")"
git -C "$Q" merge --abort
echo "  second merge: $(grep "Resolved" "$WORK/rerere3.txt")"

echo "SMOKE OK: git/merge-algorithms"
