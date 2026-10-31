#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

echo "▸ check --json runs and sees the tools CI has"
JSON="$(python3 code/main.py check --json || true)"
echo "$JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
tools = {t["name"]: t for t in d["tools"]}
assert tools["python"]["status"] == "ok", tools["python"]
assert tools["git"]["status"] == "ok", tools["git"]
assert tools["docker"]["status"] in ("ok", "old"), tools["docker"]
assert d["platform"] == "linux", d["platform"]
print("  platform", d["platform"], "pkg", d["packageManager"], "ready", d["ready"])
' || fail "check --json"

echo "▸ plan prints only what is missing"
python3 code/main.py plan | tee /tmp/lab-plan.txt
! grep -q "install git" /tmp/lab-plan.txt || fail "git is present and must not be in the plan"

echo "▸ report writes LAB.md"
python3 code/main.py report --out /tmp/LAB.md || true
grep -q "^# LAB.md" /tmp/LAB.md || fail "LAB.md header"
grep -q "## Install plan" /tmp/LAB.md || fail "LAB.md plan section"

if command -v kind >/dev/null; then
  echo "▸ main.sh creates a throwaway cluster"
  bash code/main.sh cc-lab-smoke | tee /tmp/lab-kind.txt
  grep -q "pod ok on" /tmp/lab-kind.txt || fail "pod did not run on kind"
  kind delete cluster --name cc-lab-smoke
else
  echo "  (kind not installed on this runner; cluster step skipped)"
fi

echo "SMOKE OK: setup/lab-doctor"
