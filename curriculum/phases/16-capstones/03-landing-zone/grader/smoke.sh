#!/usr/bin/env bash
# CI proof that the grader means something: solution/ must earn every point and starter/ none.
# Needs OpenTofu, Docker (the grader runs motoserver/moto) and Python 3.9+.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

MOTO_IMAGE="motoserver/moto:5.2.5"
WORK="$(mktemp -d)"
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
cleanup() {
  local ids
  ids="$(docker ps -aq --filter label=cloudcaptain.grader=landing-zone 2>/dev/null || true)"
  if [[ -n "$ids" ]]; then docker rm -f $ids > /dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

command -v tofu > /dev/null || fail "tofu is not installed"
command -v docker > /dev/null || fail "docker is not installed"
export TF_PLUGIN_CACHE_DIR="$WORK/plugins"
export TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE=1
export CC_GRADER_WORK="$WORK/grade"
mkdir -p "$TF_PLUGIN_CACHE_DIR" "$CC_GRADER_WORK"
unset LZ_AWS_ENDPOINT
echo "  $(tofu -version | head -1), $(docker --version), $(python3 --version)"

echo "▸ fill the provider cache once (aws, google, azurerm from the lock files)"
STARTED=$SECONDS
cp -R solution/modules "$WORK/warm"
for module in network network-gcp network-azure; do
  (cd "$WORK/warm/$module" && tofu init -backend=false -input=false -no-color > "$WORK/warm-$module.txt" 2>&1) || fail "tofu init in modules/$module: $(tail -20 "$WORK/warm-$module.txt")"
done
docker pull -q "$MOTO_IMAGE" > "$WORK/pull.txt" 2>&1 || fail "docker pull $MOTO_IMAGE: $(cat "$WORK/pull.txt")"
echo "  providers and $MOTO_IMAGE ready in $((SECONDS - STARTED))s"

echo "▸ grading solution/ (must earn every point)"
STARTED=$SECONDS
CODE=0
bash grader/grade.sh solution --json > "$WORK/solution.json" || CODE=$?
python3 - "$WORK/solution.json" "$CODE" <<'PY' || fail "solution/ must pass with full marks: $(cat "$WORK/solution.json")"
import json, sys
report = json.load(open(sys.argv[1]))
for m in report["milestones"]:
    print(f"  {'PASS' if m['passed'] else 'FAIL'} {m['id']:<20} {m['earned']}/{m['points']} {m['seconds']:>6}s  {m['detail']}")
assert sys.argv[2] == "0", f"exit {sys.argv[2]}"
assert report["passed"] is True and report["score"] == report["total"], (report["score"], report["total"])
PY
echo "  solution graded in $((SECONDS - STARTED))s"

echo "▸ grading starter/ (must score 0 within a minute)"
STARTED=$SECONDS
CODE=0
bash grader/grade.sh starter --json > "$WORK/starter.json" || CODE=$?
ELAPSED=$((SECONDS - STARTED))
[[ "$CODE" == "1" ]] || fail "starter/ should exit 1, got $CODE: $(cat "$WORK/starter.json")"
python3 - "$WORK/starter.json" <<'PY' || fail "starter/ must score 0: $(cat "$WORK/starter.json")"
import json, sys
report = json.load(open(sys.argv[1]))
for m in report["milestones"]:
    print(f"  {'PASS' if m['passed'] else 'FAIL'} {m['id']:<20} {m['detail'][:110]}")
assert report["score"] == 0 and not report["passed"], report["score"]
PY
[[ "$ELAPSED" -lt 60 ]] || fail "grading starter/ took ${ELAPSED}s; stubs must fail fast"
echo "  starter graded in ${ELAPSED}s"

echo "SMOKE OK: capstones/landing-zone"
