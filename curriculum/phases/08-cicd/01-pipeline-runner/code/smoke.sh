#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
LESSON="$PWD"
MINICI="$LESSON/code/main.py"
FIXTURES="$LESSON/code/fixtures"
ACT_VERSION="0.2.89"
ACTIONLINT_VERSION="1.7.12"
WF=".github/workflows/ci.yml"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
mkdir -p "$WORK/bin" "$WORK/dl" "$WORK/xdg-cache"
export PATH="$WORK/bin:$PATH"
API_TOKEN="cc-smoke-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
export API_TOKEN

sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }
no_secret() {
  if grep -rqF "$API_TOKEN" "$@"; then
    fail "the secret value appears in: $(grep -rlF "$API_TOKEN" "$@" | tr '\n' ' ')"
  fi
}
case "$(uname -s)" in
  Linux) RUNNER_LABEL=Linux; ACT_OS=Linux; AL_OS=linux ;;
  Darwin) RUNNER_LABEL=macOS; ACT_OS=Darwin; AL_OS=darwin ;;
  *) fail "unsupported OS $(uname -s)" ;;
esac
case "$(uname -m)" in
  x86_64|amd64) ACT_ARCH=x86_64; AL_ARCH=amd64 ;;
  arm64|aarch64) ACT_ARCH=arm64; AL_ARCH=arm64 ;;
  *) fail "unsupported architecture $(uname -m)" ;;
esac

cat > "$WORK/compare.py" <<'EOF'
import json
import re
import sys
from collections import Counter

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
RAW = re.compile(r"^\[[^\]]+\]\s+\|\s?(.*)$")
MARK = re.compile(r"^(CC-[A-Z]+)(?:\s+(.*))?$")
COMPARED = ("CC-MATRIX", "CC-OUTPUT", "CC-ENV", "CC-SECRET", "CC-LEAK", "CC-COVERAGE")
INFO = ("CC-SEEN", "CC-HASH")


def markers(path):
    found = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            raw = RAW.match(ANSI.sub("", line.rstrip("\n")))
            if not raw:
                continue
            mark = MARK.match(raw.group(1).strip())
            if mark and "$" not in (mark.group(2) or ""):
                found.append((index, mark.group(1), (mark.group(2) or "").strip()))
    return found


def view(found):
    jobs = Counter(value for _, kind, value in found if kind == "CC-JOB")
    first, last = {}, {}
    for index, kind, value in found:
        if kind == "CC-JOB":
            first[value] = min(first.get(value, index), index)
        if kind == "CC-DONE":
            last[value] = max(last.get(value, index), index)
    sets = {key: sorted(value for _, kind, value in found if kind == key) for key in COMPARED}
    info = {key: [value for _, kind, value in found if kind == key] for key in INFO}
    return jobs, first, last, sets, info


def order_problems(plan, first, last):
    problems = []
    for job, details in plan["jobs"].items():
        for need in details["needs"]:
            if job in first and need in last and first[job] < last[need]:
                problems.append(f"{job} started (log line {first[job]}) before {need} finished (log line {last[need]})")
    return problems


plan_path, checksum, *logs = sys.argv[1:]
with open(plan_path) as handle:
    plan = json.load(handle)
tools = dict(zip(["minici", "act"], logs))
expected_jobs = Counter({"build": 1, "test": 6, "experimental": 1, "deploy": 1, "cleanup": 1})
expected = {
    "CC-MATRIX": sorted(["os=linux python=3.11", "os=linux python=3.12", "os=macos python=3.12", "os=linux python=3.13", "os=macos python=3.13", "os=linux python=3.14"]),
    "CC-OUTPUT": [f"version=1.4.0 checksum={checksum}"],
    "CC-ENV": sorted(["flavor=release", "job-stage=job app=shop", "step-stage=step"]),
    "CC-SECRET": ["token=***"],
    "CC-LEAK": ["absent"],
    "CC-COVERAGE": ["python=3.13"],
}
views = {}
for tool, path in tools.items():
    jobs, first, last, sets, info = view(markers(path))
    if jobs != expected_jobs:
        sys.exit(f"{tool}: jobs that ran {dict(jobs)}, want {dict(expected_jobs)} (deps, package and notify are skipped)")
    problems = order_problems(plan, first, last)
    if problems:
        sys.exit(f"{tool}: needs order violated: {problems}")
    for key, want in expected.items():
        if sets[key] != want:
            sys.exit(f"{tool}: {key} lines {sets[key]}, want {want}")
    views[tool] = (jobs, sets)
    runs = ", ".join(f"{name} x{count}" for name, count in sorted(jobs.items()))
    print(f"  {tool}: ran {runs}; order valid; 6 matrix legs; {sets['CC-OUTPUT'][0]}; {sets['CC-SECRET'][0]}; leak {sets['CC-LEAK'][0]}")
    for key, values in info.items():
        print(f"    {key}: {', '.join(values)}")
if len(views) == 2:
    if views["minici"] != views["act"]:
        sys.exit(f"minici and act differ:\n  minici {views['minici']}\n  act    {views['act']}")
    print("  minici == act: the same jobs, matrix legs, outputs, env layers, masking and fresh workspaces")
EOF

echo "▸ the sample repository as a git repository (act reads the ref and the sha from git)"
command -v git >/dev/null || fail "git is required"
REPO="$WORK/repo"
cp -R "$FIXTURES/sample-repo" "$REPO"
git -C "$REPO" init -q
git -C "$REPO" symbolic-ref HEAD refs/heads/main
git -C "$REPO" add -A
git -C "$REPO" -c user.name=cc-smoke -c user.email=cc-smoke@example.invalid -c commit.gpgsign=false commit -q -m "Add: sample shop repository"
CHECKSUM="$(cksum "$REPO/src/app.py" | cut -d' ' -f1)"
git -C "$REPO" log --oneline > "$WORK/git-log.txt"
echo "  $(head -1 "$WORK/git-log.txt") on $(git -C "$REPO" symbolic-ref HEAD)"

echo "▸ minici check and plan: 9 jobs in 4 waves, 6 test legs"
python3 "$MINICI" check "$REPO/$WF" > "$WORK/check.txt" 2>&1 || fail "minici check on the sample: $(cat "$WORK/check.txt")"
python3 "$MINICI" plan "$REPO/$WF" --json > "$WORK/plan.json" || fail "minici plan on the sample"
python3 - "$WORK/plan.json" <<'PY' || fail "minici plan: $(cat "$WORK/plan.json")"
import json, sys
plan = json.load(open(sys.argv[1]))
assert plan["waves"] == [["build", "experimental", "deps"], ["test", "package"], ["deploy"], ["notify", "cleanup"]], plan["waves"]
legs = plan["jobs"]["test"]["legs"]
assert len(legs) == 6 and {"python": "3.13", "os": "linux", "coverage": True} in legs, legs
print("  waves: " + " | ".join(", ".join(wave) for wave in plan["waves"]))
PY

echo "▸ minici run 1: every job in a fresh workspace, a cache miss, an artifact, a masked secret"
CODE=0
python3 "$MINICI" run "$REPO/$WF" --runs-dir "$WORK/minici" --secret API_TOKEN --json > "$WORK/run1.json" 2> "$WORK/run1.log" || CODE=$?
[[ "$CODE" == "0" ]] || fail "minici run 1 exited $CODE: $(tail -40 "$WORK/run1.log")"
python3 - "$WORK/run1.json" "$LESSON/code" <<'PY' || fail "minici run 1 summary: $(head -c 4000 "$WORK/run1.json")"
import json, sys
sys.path.insert(0, sys.argv[2])
import main
summary = json.load(open(sys.argv[1]))
want = {"build": "success", "test": "success", "experimental": "success", "deps": "success", "package": "success", "deploy": "success", "notify": "skipped", "cleanup": "success"}
assert summary["conclusion"] == "success", summary["conclusion"]
assert summary["results"] == want, summary["results"]
experimental = [job for job in summary["jobs"] if job["job"] == "experimental"][0]
assert (experimental["outcome"], experimental["result"]) == ("failure", "success"), experimental
assert [event["event"] for event in summary["cache"]] == ["restore-miss", "save"], summary["cache"]
assert summary["artifacts"] == ["bundle"], summary["artifacts"]
assert summary["outputs"]["build"]["version"] == "1.4.0", summary["outputs"]
assert main.order_violations(summary) == [], main.order_violations(summary)
assert summary["trigger"].startswith("event push; branches matched 'main'"), summary["trigger"]
print("  " + ", ".join(f"{job}={result}" for job, result in summary["results"].items()))
print(f"  experimental: outcome failure, result success (continue-on-error); trigger: {summary['trigger']}")
PY
grep -q "CC-CACHE miss" "$WORK/run1.log" || fail "run 1 should install on a cache miss: $(grep -n deps "$WORK/run1.log")"
grep -q "CC-ARTIFACT version=1.4.0 packages=3" "$WORK/run1.log" || fail "package should read the bundle artifact: $(grep -n package "$WORK/run1.log")"
grep -q "CC-SECRET token=\*\*\*" "$WORK/run1.log" || fail "the secret should be masked: $(grep -n CC-SECRET "$WORK/run1.log")"
grep -q "CC-LEAK absent" "$WORK/run1.log" || fail "deploy should not see build's leak.txt: $(grep -n CC-LEAK "$WORK/run1.log")"

echo "▸ minici run 2: the same lock file, so the same key, so an exact cache hit"
CODE=0
python3 "$MINICI" run "$REPO/$WF" --runs-dir "$WORK/minici" --secret API_TOKEN --json > "$WORK/run2.json" 2> "$WORK/run2.log" || CODE=$?
[[ "$CODE" == "0" ]] || fail "minici run 2 exited $CODE: $(tail -40 "$WORK/run2.log")"
grep -q "CC-CACHE hit" "$WORK/run2.log" || fail "run 2 should hit the cache: $(grep -n -i cache "$WORK/run2.log")"
python3 - "$WORK/run2.json" <<'PY' || fail "run 2 cache events: $(head -c 2000 "$WORK/run2.json")"
import json, sys
summary = json.load(open(sys.argv[1]))
assert [event["event"] for event in summary["cache"]] == ["restore-hit"], summary["cache"]
print(f"  restore-hit: {summary['cache'][0]['key']}")
PY

echo "▸ hashFiles(): minici hashes each file's SHA-256 digest, the runner's algorithm"
SINGLE="$(sha256_of "$REPO/requirements.txt")"
grep -o 'CC-HASH requirements=[0-9a-f]*' "$WORK/run1.log" > "$WORK/hash-minici.txt" || true
MINI_HASH="$(head -1 "$WORK/hash-minici.txt" | cut -d= -f2)"
if command -v openssl >/dev/null; then
  DOUBLE="$(openssl dgst -sha256 -binary "$REPO/requirements.txt" | openssl dgst -sha256 | awk '{print $NF}')"
  [[ -n "$MINI_HASH" && "$MINI_HASH" == "$DOUBLE" ]] || fail "hashFiles: minici '$MINI_HASH', openssl sha256(sha256(file)) '$DOUBLE'"
  grep -q "deps-$RUNNER_LABEL-$DOUBLE" "$WORK/run1.json" || fail "the cache key should be deps-$RUNNER_LABEL-$DOUBLE: $(grep -n '"key"' "$WORK/run1.json")"
  echo "  minici $MINI_HASH = openssl sha256 of the digest; sha256sum of the file is $SINGLE"
else
  echo "  (openssl not installed; the independent hashFiles check is skipped)"
fi

echo "▸ minici trigger filters: a README-only change and a feature branch start no run"
python3 "$MINICI" run "$REPO/$WF" --runs-dir "$WORK/minici" --changed README.md > "$WORK/trigger-readme.txt" || fail "minici run (README only): $(cat "$WORK/trigger-readme.txt")"
grep -q "not triggered: paths matched none" "$WORK/trigger-readme.txt" || fail "a README-only push should not trigger: $(cat "$WORK/trigger-readme.txt")"
python3 "$MINICI" run "$REPO/$WF" --runs-dir "$WORK/minici" --ref refs/heads/feature/x --changed src/app.py > "$WORK/trigger-branch.txt" || fail "minici run (feature branch): $(cat "$WORK/trigger-branch.txt")"
grep -q "not triggered: branches did not match 'feature/x'" "$WORK/trigger-branch.txt" || fail "feature/x should not trigger: $(cat "$WORK/trigger-branch.txt")"
echo "  $(cat "$WORK/trigger-readme.txt")"
echo "  $(cat "$WORK/trigger-branch.txt")"

echo "▸ download act v$ACT_VERSION and actionlint v$ACTIONLINT_VERSION and verify their sha256"
ACT_ASSET="act_${ACT_OS}_${ACT_ARCH}.tar.gz"
ACT_BASE="https://github.com/nektos/act/releases/download/v${ACT_VERSION}"
curl -fsSL --retry 3 -o "$WORK/dl/$ACT_ASSET" "$ACT_BASE/$ACT_ASSET" || fail "download $ACT_BASE/$ACT_ASSET"
curl -fsSL --retry 3 -o "$WORK/dl/act-checksums.txt" "$ACT_BASE/checksums.txt" || fail "download $ACT_BASE/checksums.txt"
WANT="$(awk -v f="$ACT_ASSET" '$2 == f {print $1}' "$WORK/dl/act-checksums.txt")"
GOT="$(sha256_of "$WORK/dl/$ACT_ASSET")"
[[ -n "$WANT" && "$WANT" == "$GOT" ]] || fail "act checksum mismatch: want '$WANT' got '$GOT'"
tar -xzf "$WORK/dl/$ACT_ASSET" -C "$WORK/bin" act
AL_ASSET="actionlint_${ACTIONLINT_VERSION}_${AL_OS}_${AL_ARCH}.tar.gz"
AL_BASE="https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}"
curl -fsSL --retry 3 -o "$WORK/dl/$AL_ASSET" "$AL_BASE/$AL_ASSET" || fail "download $AL_BASE/$AL_ASSET"
curl -fsSL --retry 3 -o "$WORK/dl/actionlint-checksums.txt" "$AL_BASE/actionlint_${ACTIONLINT_VERSION}_checksums.txt" || fail "download the actionlint checksums"
WANT="$(awk -v f="$AL_ASSET" '$2 == f {print $1}' "$WORK/dl/actionlint-checksums.txt")"
GOT="$(sha256_of "$WORK/dl/$AL_ASSET")"
[[ -n "$WANT" && "$WANT" == "$GOT" ]] || fail "actionlint checksum mismatch: want '$WANT' got '$GOT'"
tar -xzf "$WORK/dl/$AL_ASSET" -C "$WORK/bin" actionlint
chmod +x "$WORK/bin/act" "$WORK/bin/actionlint"
act --version > "$WORK/versions.txt" 2>&1
actionlint -version >> "$WORK/versions.txt" 2>&1
head -2 "$WORK/versions.txt" | sed 's/^/  /'

echo "▸ actionlint: the sample is clean, and each broken fixture fails for the reason minici check gives"
if command -v shellcheck >/dev/null; then
  echo "  (shellcheck found: actionlint also checks every run: script)"
else
  echo "  (shellcheck not installed; actionlint skips the run: script checks)"
fi
actionlint -no-color "$REPO/$WF" > "$WORK/actionlint-sample.txt" 2>&1 || fail "actionlint on the sample: $(cat "$WORK/actionlint-sample.txt")"
check_both() {
  local file="$1" lint_token="$2" mini_token="$3" mini_want="$4" code=0
  actionlint -no-color "$FIXTURES/broken/$file" > "$WORK/actionlint-$file.txt" 2>&1 || code=$?
  [[ "$code" == "1" ]] && grep -q "$lint_token" "$WORK/actionlint-$file.txt" || fail "actionlint on $file (exit $code): $(cat "$WORK/actionlint-$file.txt")"
  code=0
  python3 "$MINICI" check "$FIXTURES/broken/$file" > "$WORK/minici-$file.txt" 2>&1 || code=$?
  [[ "$code" == "$mini_want" ]] && grep -q "$mini_token" "$WORK/minici-$file.txt" || fail "minici check on $file (exit $code, want $mini_want): $(cat "$WORK/minici-$file.txt")"
  echo "  $file: actionlint '$lint_token'; minici '$mini_token' (exit $mini_want)"
}
check_both needs-typo.yml "does not exist" "does not exist" 2
check_both needs-cycle.yml "cyclic dependencies" "needs cycle" 2
check_both if-always-true.yml "always evaluated to true" "always true" 1

echo "▸ act -l: act plans the same waves (its stages count from 0)"
(cd "$REPO" && act -l -W "$WF" < /dev/null) > "$WORK/act-list.txt" 2>&1 || fail "act -l: $(cat "$WORK/act-list.txt")"
python3 - "$WORK/act-list.txt" "$WORK/plan.json" <<'PY' || fail "act -l stages differ from minici plan: $(cat "$WORK/act-list.txt")"
import json, re, sys
lines = [re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line.rstrip("\n")) for line in open(sys.argv[1], encoding="utf-8", errors="replace")]
start = next(index for index, line in enumerate(lines) if "Stage" in line and "Job ID" in line)
header = re.split(r"\s{2,}", lines[start].strip())
stage_column, job_column = header.index("Stage"), header.index("Job ID")
stages = {}
for line in lines[start + 1 :]:
    cells = re.split(r"\s{2,}", line.strip())
    if len(cells) == len(header) and cells[stage_column].isdigit():
        stages[cells[job_column]] = int(cells[stage_column])
plan = json.load(open(sys.argv[2]))
mine = {job: details["wave"] - 1 for job, details in plan["jobs"].items()}
assert stages == mine, (stages, mine)
print("  act stages == minici waves: " + ", ".join(f"{job}={stage}" for job, stage in sorted(stages.items())))
PY

echo "▸ minici run 3 with act's event payload: deps and package skip, as they will under act"
CODE=0
python3 "$MINICI" run "$REPO/$WF" --runs-dir "$WORK/minici" --secret API_TOKEN --event-path "$FIXTURES/act-event.json" --json > "$WORK/run3.json" 2> "$WORK/minici-act.log" || CODE=$?
[[ "$CODE" == "0" ]] || fail "minici run 3 exited $CODE: $(tail -40 "$WORK/minici-act.log")"
python3 - "$WORK/run3.json" <<'PY' || fail "run 3 results: $(head -c 2000 "$WORK/run3.json")"
import json, sys
results = json.load(open(sys.argv[1]))["results"]
assert (results["deps"], results["package"], results["notify"]) == ("skipped", "skipped", "skipped"), results
PY

echo "▸ act push in self-hosted mode: the same workflow, every step run on this machine"
ACT_CODE=0
(cd "$REPO" && XDG_CACHE_HOME="$WORK/xdg-cache" act push -W "$WF" -e "$FIXTURES/act-event.json" -P ubuntu-latest=-self-hosted -s API_TOKEN --no-cache-server < /dev/null) > "$WORK/act.log" 2>&1 || ACT_CODE=$?
echo "  act exited $ACT_CODE"
grep -q "CC-JOB build" "$WORK/act.log" || fail "act did not run the build job: $(tail -60 "$WORK/act.log")"
grep -F "Job failed" "$WORK/act.log" > "$WORK/act-failed.txt" || true
if grep -v "experimental" "$WORK/act-failed.txt" > "$WORK/act-other-failures.txt"; then
  fail "act: a job other than experimental failed: $(cat "$WORK/act-other-failures.txt"); log tail: $(tail -60 "$WORK/act.log")"
fi
if [[ "$ACT_CODE" != "0" ]]; then
  grep -q "experimental" "$WORK/act-failed.txt" || fail "act exited $ACT_CODE without a failed job: $(tail -60 "$WORK/act.log")"
  echo "  act has no job-level continue-on-error, so the experimental failure fails its run; GitHub and minici pass it"
fi

echo "▸ minici and act: the same jobs in a valid order, the same legs, outputs, env layers and masking"
python3 "$WORK/compare.py" "$WORK/plan.json" "$CHECKSUM" "$WORK/minici-act.log" "$WORK/act.log" || fail "minici and act disagree; act log tail: $(tail -60 "$WORK/act.log")"
grep -o 'CC-HASH requirements=[0-9a-f]*' "$WORK/act.log" > "$WORK/hash-act.txt" || true
ACT_HASH="$(head -1 "$WORK/hash-act.txt" | cut -d= -f2)"
if [[ "$ACT_HASH" == "$SINGLE" ]]; then
  echo "  hashFiles(): act $ACT_HASH hashes the contents; the runner and minici hash the digest ($MINI_HASH)"
else
  echo "  (note: act's hashFiles() gave '$ACT_HASH'; sha256sum of the file is $SINGLE)"
fi

echo "▸ the secret value never appears in any log, summary or cache file"
no_secret "$WORK/act.log" "$WORK/minici-act.log" "$WORK/run1.log" "$WORK/run2.log" "$WORK/run1.json" "$WORK/run2.json" "$WORK/run3.json" "$WORK/minici"

echo "SMOKE OK: cicd/pipeline-runner"
