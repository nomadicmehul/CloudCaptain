#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
LESSON="$PWD"
MB="$LESSON/code/main.py"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
command -v git >/dev/null || fail "git is not installed"
command -v python3 >/dev/null || fail "python3 is not installed"

export GIT_CONFIG_GLOBAL="$WORK/gitconfig" GIT_CONFIG_NOSYSTEM=1
: > "$GIT_CONFIG_GLOBAL"
git config --global init.defaultBranch main
git config --global commit.gpgsign false
git config --global gc.auto 0
git config --global advice.detachedHead false
git config --global user.name "Ada Lovelace"
git config --global user.email ada@example.invalid
echo "▸ $(git version), $(python3 --version)"

mb() { python3 "$MB" "$@"; }

cat > "$WORK/check.py" <<'PY'
import json
import os
import random
import re
import subprocess
import sys

HEX = re.compile(r"^[0-9a-f]{40}$")
PEOPLE = {"ada": ("Ada Lovelace", "ada@example.invalid"), "grace": ("Grace Hopper", "grace@example.invalid"),
          "linus": ("Linus Torvalds", "linus@example.invalid"), "margaret": ("Margaret Hamilton", "margaret@example.invalid")}


def die(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def read(path):
    with open(path, encoding="utf-8", errors="surrogateescape") as handle:
        return handle.read()


class Repo:
    def __init__(self, path, start):
        self.path, self.clock = path, int(start)

    def git(self, *args, who="ada"):
        name, mail = PEOPLE[who]
        env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=mail, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=mail,
                   GIT_AUTHOR_DATE=f"@{self.clock} +0100", GIT_COMMITTER_DATE=f"@{self.clock} +0100")
        done = subprocess.run(["git", "-C", self.path, *args], env=env, capture_output=True, text=True)
        if done.returncode:
            die(f"git {' '.join(args)}: {done.stdout}{done.stderr}")
        return done.stdout.strip()

    def write(self, name, text):
        with open(os.path.join(self.path, name), "w", encoding="utf-8") as handle:
            handle.write(text)


def bisect_repo(path, bug, *broken):
    repo = Repo(path, 1767600000)
    os.makedirs(path)
    repo.git("init", "-q")

    def commit(name):
        repo.clock += 60
        if name == bug:
            repo.write("app.py", "THRESHOLD = 7\n\ndef check(value):\n    return value < THRESHOLD\n")
        branch = name.rsplit(" ", 1)[0]
        with open(os.path.join(path, branch + ".log"), "a", encoding="utf-8") as handle:
            handle.write(name + "\n")
        marker = os.path.join(path, "BROKEN")
        if name in broken:
            repo.write("BROKEN", "does not build\n")
        elif os.path.exists(marker):
            os.remove(marker)
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", name)

    def merge(branch):
        repo.clock += 60
        repo.git("merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)

    repo.write("app.py", "THRESHOLD = 10\n\ndef check(value):\n    return value < THRESHOLD\n")
    commit("initial 0")
    for n in range(1, 9):
        commit(f"main {n}")
    repo.git("branch", "feature-a")
    for n in range(9, 13):
        commit(f"main {n}")
    repo.git("checkout", "-q", "feature-a")
    for n in range(1, 7):
        commit(f"feature-a {n}")
    repo.git("checkout", "-q", "-b", "feature-b", "main")
    repo.git("checkout", "-q", "main")
    for n in range(13, 15):
        commit(f"main {n}")
    merge("feature-a")
    repo.git("checkout", "-q", "feature-b")
    for n in range(1, 9):
        commit(f"feature-b {n}")
    repo.git("checkout", "-q", "main")
    for n in range(15, 23):
        commit(f"main {n}")
    merge("feature-b")
    for n in range(23, 31):
        commit(f"main {n}")
    print(repo.git("rev-list", "--max-parents=0", "HEAD"))


def blame_repo(path):
    repo = Repo(path, 1767700000)
    os.makedirs(path)
    repo.git("init", "-q")

    def commit(who, message, lines):
        repo.clock += 3600
        repo.write("service.py", "".join(line + "\n" for line in lines))
        repo.git("add", "service.py", who=who)
        repo.git("commit", "-q", "-m", message, who=who)
        return repo.git("rev-parse", "HEAD")

    def edit(lines, old, new):
        if old not in lines:
            die(f"blame history: {old!r} not found")
        return [new if line == old else line for line in lines]

    v = ["import os", "import sys", "", "TIMEOUT=30", "RETRIES=3", "",
         "def connect(host, port):", "    address=(host, port)", "    return open_socket(address, TIMEOUT)", "",
         "def fetch(client, path):", "    response=client.get(path)", "    return response.body", "",
         "def main():", "    client=connect('localhost', 8080)", "    print(fetch(client, '/health'))", "",
         "if __name__ == '__main__':", "    main()"]
    commit("ada", "Initial service", v)
    v = edit(v, "RETRIES=3", "RETRIES=5")
    v = v[:14] + ["def close(client):", "    client.shutdown(reason='done')", "    return None", ""] + v[14:]
    commit("grace", "Add close() and more retries", v)
    repo.git("checkout", "-q", "-b", "feature")
    f = edit(v, "    print(fetch(client, '/health'))", "    print(fetch(client, '/ready'))")
    commit("linus", "Use readiness endpoint and json", f[:2] + ["import json"] + f[2:])
    repo.git("checkout", "-q", "main")
    commit("ada", "Lower timeout", edit(v, "TIMEOUT=30", "TIMEOUT=10"))
    repo.clock += 3600
    repo.git("merge", "-q", "--no-ff", "-m", "Merge feature", "feature", who="grace")
    v = read(os.path.join(path, "service.py")).splitlines()
    v = [line.replace("=", " = ") if "=" in line and "==" not in line and "reason=" not in line else line for line in v]
    v = edit(v, "    client.shutdown(reason='done')", "    client.shutdown(")
    v.insert(v.index("    client.shutdown(") + 1, "        reason='done')")
    v.insert(v.index("def fetch(client, path):"), "")
    formatting = commit("margaret", "Format with black", v)
    start = v.index("def connect(host, port):")
    block, v = v[start:start + 4], v[:start] + v[start + 4:]
    at = v.index("if __name__ == '__main__':")
    commit("grace", "Move connect() next to main", v[:at] + block + v[at:])
    v = read(os.path.join(path, "service.py")).splitlines()
    commit("linus", "Return parsed JSON", edit(v, "    return response.body", "    return response.json()"))
    repo.write(".git-blame-ignore-revs", f"# Format with black\n{formatting}\n")
    print(formatting)


def states(path, seed, count):
    out = subprocess.run(["git", "-C", path, "log", "--all", "--format=%H %P"], capture_output=True, text=True, check=True).stdout
    parents = {line.split()[0]: line.split()[1:] for line in out.splitlines()}

    def ancestors(tips):
        seen, stack = set(), list(tips)
        while stack:
            sha = stack.pop()
            if sha not in seen:
                seen.add(sha)
                stack.extend(parents[sha])
        return seen

    rng = random.Random(int(seed))
    commits = sorted(parents)
    made = 0
    while made < int(count):
        bad = rng.choice(commits)
        goods = rng.sample(commits, rng.choice([1, 1, 2, 3]))
        if bad in ancestors(goods):
            continue
        print(bad, *goods)
        made += 1


def bisect_vars(vars_file, all_file, mb_json):
    found = dict(line.split("=", 1) for line in read(vars_file).splitlines() if "=" in line)
    found = {key: value.strip("'") for key, value in found.items()}
    data = json.loads(read(mb_json))
    distances = {}
    for line in read(all_file).splitlines():
        match = re.match(r"^([0-9a-f]{40}) \((?:.*, )?dist=(-?\d+)\)", line)
        if match:
            distances[match[1]] = int(match[2])
    if distances != data["distances"]:
        die(f"distances differ: git rev-list --bisect-all {distances} minibisect {data['distances']}")
    if data["state"] == "found":
        if found["bisect_rev"] != data["commit"]:
            die(f"git picks {found['bisect_rev']}, minibisect says {data['commit']} is the first bad commit")
        print("found")
        return
    mine = {"bisect_rev": data["commit"], "bisect_good": str(data["remaining"]), "bisect_bad": str(data["reaches"] - 1),
            "bisect_all": str(data["candidates"]), "bisect_steps": str(data["steps"])}
    for key, value in mine.items():
        if found.get(key) != value:
            die(f"{key}: git rev-list --bisect-vars {found.get(key)}, minibisect {value}")
    print("test")


def bisect_log(path):
    trials, possible, first = [], [], None
    for line in read(path).splitlines():
        match = re.match(r"^git bisect (good|bad|skip) ([0-9a-f]{40})$", line)
        if match:
            trials.append([match[1], match[2]])
        match = re.match(r"^# (possible )?first bad commit: \[([0-9a-f]{40})\]", line)
        if match:
            if match[1]:
                possible.append(match[2])
            else:
                first = match[2]
    return trials, sorted(possible), first


def samerun(git_log, mb_json, expected):
    trials, possible, first = bisect_log(git_log)
    data = json.loads(read(mb_json))
    mine = [[t["verdict"], t["commit"]] for t in data["log"]]
    if trials != mine:
        die("the two runs tested different commits:\n git bisect log: " + json.dumps(trials) + "\n minibisect:     " + json.dumps(mine))
    if data["state"] == "found":
        if first != data["commit"]:
            die(f"git found {first}, minibisect {data['commit']}")
        if expected and first != expected:
            die(f"both found {first}, the planted bug is {expected}")
        result = first
    else:
        if sorted(data["suspects"]) != possible:
            die(f"only skipped commits left: git lists {possible}, minibisect {sorted(data['suspects'])}")
        result = "only-skipped:" + ",".join(s[:7] for s in possible)
    skips = [sha for verdict, sha in trials if verdict == "skip"]
    print(f"{len(trials)} {len(skips)} {result}")


def blamecmp(porcelain, mb_json, label):
    records, current = [], None
    for line in read(porcelain).splitlines(keepends=True):
        match = re.match(r"^([0-9a-f]{40}) (\d+) (\d+)", line)
        if match:
            current = {"commit": match[1], "orig": int(match[2]), "line": int(match[3]), "flags": set(), "author": None}
        elif line.startswith("\t"):
            current["text"] = line[1:]
            records.append(current)
        elif line.startswith("author "):
            current["author"] = line[7:].strip()
        elif line.strip() in ("boundary", "ignored", "unblamable"):
            current["flags"].add(line.strip())
    mine = json.loads(read(mb_json))["lines"]
    if len(records) != len(mine):
        die(f"{label}: git blames {len(records)} lines, minibisect {len(mine)}")
    for theirs, ours in zip(records, mine):
        flags = {name for name in ("boundary", "ignored", "unblamable") if ours[name]}
        if (theirs["commit"], theirs["orig"], theirs["line"], theirs["flags"]) != (ours["commit"], ours["orig_line"], ours["line"], flags):
            die(f"{label}: line {theirs['line']} {theirs['text']!r}: git {theirs['commit'][:8]} line {theirs['orig']} {sorted(theirs['flags'])}, minibisect {ours['commit'][:8]} line {ours['orig_line']} {sorted(flags)}")
    authors = {r["author"] for r in records}
    flagged = sum(1 for r in records if r["flags"] - {"boundary"})
    print(f"{len(records)} lines, {len({r['commit'] for r in records})} commits, {len(authors)} authors, {flagged} ignored/unblamable")


def pickcmp(git_file, mb_json):
    theirs = read(git_file).split()
    mine = [item["commit"] for item in json.loads(read(mb_json))]
    if theirs != mine:
        die(f"git log {theirs} minibisect {mine}")
    print(len(theirs))


def lostcmp(no_reflogs, with_reflogs, mb_json):
    def dangling(path):
        return {line.split()[2] for line in read(path).splitlines() if line.startswith("dangling commit ")}
    data = json.loads(read(mb_json))
    tips = {item["commit"] for item in data}
    flagged = {item["commit"] for item in data if item["dangling"]}
    if tips != dangling(no_reflogs):
        die(f"git fsck --no-reflogs dangling {sorted(dangling(no_reflogs))}, minibisect lost {sorted(tips)}")
    if flagged != dangling(with_reflogs):
        die(f"git fsck dangling {sorted(dangling(with_reflogs))}, minibisect dangling {sorted(flagged)}")
    print(f"{len(tips)} {len(flagged)}")


def field(mb_json, subject, key):
    for item in json.loads(read(mb_json)):
        if item["subject"] == subject:
            print(item[key] if not isinstance(item[key], list) else len(item[key]))
            return
    die(f"no lost commit with subject {subject!r} in {read(mb_json)}")


COMMANDS = {"bisect-repo": bisect_repo, "blame-repo": blame_repo, "states": states, "vars": bisect_vars, "samerun": samerun,
            "blamecmp": blamecmp, "pickcmp": pickcmp, "lostcmp": lostcmp, "field": field}
COMMANDS[sys.argv[1]](*sys.argv[2:])
PY
check() { python3 "$WORK/check.py" "$@"; }

cat > "$WORK/test.sh" <<'SH'
#!/bin/sh
[ -e BROKEN ] && exit 125
grep -q '^THRESHOLD = 10$' app.py && exit 0
exit 1
SH

echo "▸ next: minibisect picks the same midpoint, distances and step estimate as git rev-list --bisect-vars/--bisect-all on 30 random bisection states"
R="$WORK/states"
check bisect-repo "$R" "feature-b 4" > /dev/null || fail "history generation"
check states "$R" 20261111 30 > "$WORK/states.txt" || fail "state generation"
FOUND=0 TESTS=0
while read -r BAD GOODS; do
  NOT=()
  for g in $GOODS; do NOT+=("^$g"); done
  git -C "$R" rev-list --bisect-vars "$BAD" "${NOT[@]}" > "$WORK/vars.txt" || fail "git rev-list --bisect-vars $BAD ${NOT[*]}"
  git -C "$R" rev-list --bisect-all "$BAD" "${NOT[@]}" > "$WORK/all.txt" || fail "git rev-list --bisect-all"
  GOODARGS=()
  for g in $GOODS; do GOODARGS+=(--good "$g"); done
  mb next --repo "$R" --bad "$BAD" "${GOODARGS[@]}" --json > "$WORK/next.json" 2> "$WORK/next.err" || fail "minibisect next: $(cat "$WORK/next.err")"
  KIND="$(check vars "$WORK/vars.txt" "$WORK/all.txt" "$WORK/next.json")" || fail "state bad=$BAD goods=$GOODS"
  if [[ "$KIND" == "found" ]]; then FOUND=$((FOUND + 1)); else TESTS=$((TESTS + 1)); fi
done < "$WORK/states.txt"
echo "  30 states agree ($TESTS with a commit to test, $FOUND already down to the first bad commit)"

echo "▸ run: git bisect run and minibisect run with the same test script name the same first bad commit, skip the same untestable commits, test the same sequence"
SCENARIOS=(
  "feature-b 4|main 5|main 16|feature-b 6|main 25"
  "main 10|main 16|main 11|feature-a 2|main 12"
  "feature-a 3|feature-a 3|main 13|feature-a 4"
  "main 27|main 16|main 24|main 29|feature-b 2"
)
SKIPPED=0 ONLY=0
for spec in "${SCENARIOS[@]}"; do
  IFS='|' read -r -a PARTS <<< "$spec"
  BUG="${PARTS[0]}"
  S="$WORK/scenario-$(echo "$BUG" | tr ' ' '-')"
  ROOT="$(check bisect-repo "$S" "${PARTS[@]}")" || fail "history for $spec"
  EXPECTED="$(git -C "$S" log --all --format='%H %s' | awk -v s="$BUG" '{h=$1; $1=""; if (substr($0, 2) == s) print h}')"
  BADTIP="$(git -C "$S" rev-parse main)"
  git -C "$S" bisect start "$BADTIP" "$ROOT" > "$WORK/start.txt" 2>&1 || fail "git bisect start: $(cat "$WORK/start.txt")"
  CODE=0
  git -C "$S" bisect run sh "$WORK/test.sh" > "$WORK/git-run.txt" 2>&1 || CODE=$?
  git -C "$S" bisect log > "$WORK/git-log.txt"
  git -C "$S" bisect reset > /dev/null 2>&1
  CODE=0
  mb run --repo "$S" --bad main --good "$ROOT" --json -- sh "$WORK/test.sh" > "$WORK/mb-run.json" 2> "$WORK/mb-run.err" || CODE=$?
  [[ "$CODE" == "0" || "$CODE" == "1" ]] || fail "minibisect run exit $CODE: $(cat "$WORK/mb-run.err")"
  [[ "$(git -C "$S" rev-parse --abbrev-ref HEAD)" == "main" ]] || fail "minibisect run should restore the original branch"
  if grep -q "only 'skip'ped commits left" "$WORK/git-run.txt"; then WANT=""; else WANT="$EXPECTED"; fi
  OUT="$(check samerun "$WORK/git-log.txt" "$WORK/mb-run.json" "$WANT")" || fail "scenario '$spec' (git: $(tail -5 "$WORK/git-run.txt"))"
  read -r TRIALS SKIPS RESULT <<< "$OUT"
  SKIPPED=$((SKIPPED + SKIPS))
  if [[ "$RESULT" == only-skipped:* ]]; then ONLY=$((ONLY + 1)); fi
  echo "  bug in '$BUG': $TRIALS tests, $SKIPS skipped, result $(echo "$RESULT" | cut -c1-40)"
done
[[ "$SKIPPED" -ge 3 ]] || fail "the scenarios should make both tools skip untestable commits at least 3 times, saw $SKIPPED"
[[ "$ONLY" == "1" ]] || fail "exactly one scenario (bug in an untestable commit) should end with only skipped commits left, saw $ONLY"

echo "▸ Use It: a manual session (git bisect start/good/bad/skip) follows minibisect's verdicts and checks out minibisect's next commit every time"
S="$WORK/scenario-feature-b-4"
ROOT="$(git -C "$S" rev-list --max-parents=0 main)"
mb run --repo "$S" --bad main --good "$ROOT" --json -- sh "$WORK/test.sh" > "$WORK/manual.json" 2> /dev/null || fail "minibisect run for the manual session"
python3 -c 'import json,sys; [print(t["verdict"], t["commit"]) for t in json.load(open(sys.argv[1]))["log"]]' "$WORK/manual.json" > "$WORK/verdicts.txt"
git -C "$S" bisect start > /dev/null
git -C "$S" bisect bad main > /dev/null
git -C "$S" bisect good "$ROOT" > "$WORK/manual.txt" 2>&1
while read -r VERDICT SHA; do
  [[ "$(git -C "$S" rev-parse HEAD)" == "$SHA" ]] || fail "git checked out $(git -C "$S" rev-parse HEAD), minibisect tested $SHA next"
  git -C "$S" bisect "$VERDICT" > "$WORK/manual.txt" 2>&1 || true
done < "$WORK/verdicts.txt"
grep -q "is the first bad commit" "$WORK/manual.txt" || fail "manual session should end with the first bad commit: $(cat "$WORK/manual.txt")"
git -C "$S" bisect log > "$WORK/manual-log.txt"
git -C "$S" bisect reset > /dev/null 2>&1
git -C "$S" bisect replay "$WORK/manual-log.txt" > "$WORK/replay.txt" 2>&1 || true
grep -q "is the first bad commit" "$WORK/replay.txt" || fail "git bisect replay should reach the same result: $(cat "$WORK/replay.txt")"
git -C "$S" bisect reset > /dev/null 2>&1
echo "  $(wc -l < "$WORK/verdicts.txt" | tr -d ' ') steps, $(grep -c skip "$WORK/verdicts.txt" || true) of them skip; git bisect log replays to: $(grep 'is the first bad commit' "$WORK/replay.txt" | cut -c1-12)"

echo "▸ blame: minibisect blame equals git blame --line-porcelain line by line (commit, original line, ignored/unblamable flags) for a file with four authors"
B="$WORK/blame"
FORMAT="$(check blame-repo "$B")" || fail "blame history"
G=(git -C "$B" -c blame.markIgnoredLines=true -c blame.markUnblamableLines=true -c diff.indentHeuristic=false blame --line-porcelain)
for mode in plain ignore-revs-file move ignore-and-move; do
  case "$mode" in
    plain) FLAGS=() ;;
    ignore-revs-file) FLAGS=(--ignore-revs-file "$B/.git-blame-ignore-revs") ;;
    move) FLAGS=(-M) ;;
    ignore-and-move) FLAGS=(-M --ignore-revs-file "$B/.git-blame-ignore-revs") ;;
  esac
  "${G[@]}" "${FLAGS[@]}" service.py > "$WORK/git-blame.txt" 2> "$WORK/git-blame.err" || fail "git blame $mode: $(cat "$WORK/git-blame.err")"
  mb blame --repo "$B" --json "${FLAGS[@]}" service.py > "$WORK/mb-blame.json" 2> "$WORK/mb-blame.err" || fail "minibisect blame $mode: $(cat "$WORK/mb-blame.err")"
  SUMMARY="$(check blamecmp "$WORK/git-blame.txt" "$WORK/mb-blame.json" "$mode")" || fail "blame $mode"
  [[ "$mode" != plain || "$(echo "$SUMMARY" | awk '{print $5}')" -ge 3 ]] || fail "the blamed file should have at least three authors: $SUMMARY"
  echo "  $mode: $SUMMARY"
done
for rev in HEAD~1 HEAD~2 HEAD~3; do
  "${G[@]}" "$rev" -- service.py > "$WORK/git-blame.txt"
  mb blame --repo "$B" --json "$rev" service.py > "$WORK/mb-blame.json" || fail "minibisect blame $rev"
  check blamecmp "$WORK/git-blame.txt" "$WORK/mb-blame.json" "$rev" > /dev/null || fail "blame at $rev"
done
mb blame --repo "$B" --ignore-revs-file "$B/.git-blame-ignore-revs" service.py > "$WORK/human.txt"
if grep "Margaret" "$WORK/human.txt" | grep -qv '^\*'; then fail "with the formatting commit ignored only unblamable (*) lines may stay with its author: $(cat "$WORK/human.txt")"; fi
grep -q '^\*' "$WORK/human.txt" || fail "the blank line the formatter added has no parent line and should be marked unblamable: $(cat "$WORK/human.txt")"
grep -q "Margaret Hamilton" <(mb blame --repo "$B" service.py) || fail "without the ignore list the formatting commit owns lines"
git -C "$B" config blame.ignoreRevsFile .git-blame-ignore-revs
git -C "$B" -c blame.markIgnoredLines=true -c blame.markUnblamableLines=true -c diff.indentHeuristic=false blame --line-porcelain service.py > "$WORK/git-config.txt"
mb blame --repo "$B" --json --ignore-revs-file "$B/.git-blame-ignore-revs" service.py > "$WORK/mb-config.json"
check blamecmp "$WORK/git-config.txt" "$WORK/mb-config.json" "blame.ignoreRevsFile" > /dev/null || fail "blame.ignoreRevsFile should act like --ignore-revs-file"
git -C "$B" -c blame.markIgnoredLines=true -c blame.markUnblamableLines=true blame --line-porcelain service.py > "$WORK/git-default.txt"
mb blame --repo "$B" --json --ignore-revs-file "$B/.git-blame-ignore-revs" service.py > "$WORK/mb-default.json"
if check blamecmp "$WORK/git-default.txt" "$WORK/mb-default.json" "default" > /dev/null 2>&1; then
  echo "  also identical with git's indent heuristic on (no ambiguous slides in this file); formatting commit ${FORMAT:0:8} ignored via blame.ignoreRevsFile"
else
  echo "  (with the indent heuristic on, git placed an ambiguous change differently; minibisect has no indent heuristic)"
fi

echo "▸ pickaxe: minibisect pickaxe -S and -G list the same commits as git log -S and -G, merges excluded"
P=0
for repo in "$B" "$WORK/scenario-feature-b-4"; do
  for needle in "TIMEOUT" "RETRIES = 5" "json" "connect(" "THRESHOLD = 7" "THRESHOLD" "feature-a 3"; do
    git -C "$repo" log --format=%H -S"$needle" > "$WORK/git-s.txt"
    mb pickaxe --repo "$repo" -S "$needle" --json > "$WORK/mb-s.json" || true
    check pickcmp "$WORK/git-s.txt" "$WORK/mb-s.json" > /dev/null || fail "-S '$needle' in $(basename "$repo")"
    P=$((P + 1))
  done
  for regex in "TIMEOUT" "^def " "shutdown" "response" "THRESHOLD = [0-9]+" "^main 1[0-9]$" "does not build"; do
    git -C "$repo" log --format=%H -G"$regex" > "$WORK/git-g.txt"
    mb pickaxe --repo "$repo" -G "$regex" --json > "$WORK/mb-g.json" || true
    check pickcmp "$WORK/git-g.txt" "$WORK/mb-g.json" > /dev/null || fail "-G '$regex' in $(basename "$repo")"
    P=$((P + 1))
  done
done
N="$(check pickcmp <(git -C "$WORK/scenario-feature-b-4" log --format=%H -S"THRESHOLD = 7") <(mb pickaxe --repo "$WORK/scenario-feature-b-4" -S "THRESHOLD = 7" --json))"
[[ "$N" == "1" ]] || fail "-S 'THRESHOLD = 7' should find only the commit that planted the bug, not the merge that brought it in"
echo "  $P searches agree; -S 'THRESHOLD = 7' finds only the commit that planted the bug"

echo "▸ lost commits: a deleted branch, a dropped stash and a commit nothing points to; minibisect lost agrees with git fsck and the branch comes back"
L="$WORK/lost"
git init -q "$L"
export GIT_AUTHOR_DATE="@1767800000 +0100" GIT_COMMITTER_DATE="@1767800000 +0100"
echo base > "$L/a" && git -C "$L" add a && git -C "$L" commit -q -m base
git -C "$L" checkout -q -b experiment
echo one > "$L/b" && git -C "$L" add b && git -C "$L" commit -q -m "experiment 1"
echo two >> "$L/b" && git -C "$L" commit -q -am "experiment 2"
TIP="$(git -C "$L" rev-parse HEAD)"
git -C "$L" checkout -q main
git -C "$L" branch -D experiment > /dev/null
echo wip >> "$L/a" && git -C "$L" stash -q && git -C "$L" stash drop -q
ORPHAN="$(git -C "$L" commit-tree "$(git -C "$L" write-tree)" -m orphan)"
unset GIT_AUTHOR_DATE GIT_COMMITTER_DATE
git -C "$L" fsck --no-reflogs > "$WORK/fsck-no-reflogs.txt" 2>/dev/null || true
git -C "$L" fsck > "$WORK/fsck.txt" 2>/dev/null || true
mb lost --repo "$L" --json > "$WORK/lost.json" || fail "minibisect lost"
COUNTS="$(check lostcmp "$WORK/fsck-no-reflogs.txt" "$WORK/fsck.txt" "$WORK/lost.json")" || fail "lost commits"
FOUNDTIP="$(check field "$WORK/lost.json" "experiment 2" commit)" || fail "the deleted branch's tip should be listed"
[[ "$FOUNDTIP" == "$TIP" ]] || fail "minibisect found $FOUNDTIP, the branch tip was $TIP"
[[ "$(check field "$WORK/lost.json" "experiment 2" reflog)" -ge 1 ]] || fail "the branch tip should be found through HEAD's reflog"
[[ "$(check field "$WORK/lost.json" orphan commit)" == "$ORPHAN" ]] || fail "the orphan commit should be listed"
git -C "$L" reflog show --format='%H %gs' > "$WORK/reflog.txt"
grep -q "^$TIP " "$WORK/reflog.txt" || fail "git reflog should still list $TIP"
git -C "$L" branch rescued "$TIP"
[[ "$(git -C "$L" cat-file -t rescued)" == "commit" ]] || fail "git cat-file -t rescued"
git -C "$L" cat-file -p rescued > "$WORK/rescued.txt"
grep -q "^experiment 2$" "$WORK/rescued.txt" || fail "rescued commit message: $(cat "$WORK/rescued.txt")"
[[ "$(git -C "$L" show rescued:b | wc -l | tr -d ' ')" == "2" ]] || fail "rescued branch should have both experiment commits"
git -C "$L" fsck --lost-found > /dev/null 2>&1 || true
[[ -f "$L/.git/lost-found/commit/$ORPHAN" ]] || fail "git fsck --lost-found should write the orphan to .git/lost-found/commit"
read -r TIPS DANGLING <<< "$COUNTS"
echo "  $TIPS lost tips (= git fsck --no-reflogs), $DANGLING dangling (= git fsck); experiment 2 recovered as branch 'rescued' from HEAD's reflog"

echo "SMOKE OK: git/bisect-blame"
