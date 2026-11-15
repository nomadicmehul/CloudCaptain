#!/usr/bin/env bash
# CI proof that the grader means something: solution/ must earn every point and starter/ none.
# Needs a kind cluster as the current kubectl context (lesson-smoke.yml provides kind-cc-smoke).
# The grader starts everything else itself: the incident namespaces and the fake model server.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
cleanup() {
  kubectl get namespaces -l cloudcaptain.io/grader=copilot -o name > "$WORK/left.txt" 2>/dev/null || true
  while read -r ns; do
    [[ -n "$ns" ]] && kubectl delete "$ns" --wait=false >/dev/null 2>&1 || true
  done < "$WORK/left.txt"
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "▸ kubectl and a kind cluster"
command -v kubectl >/dev/null || fail "kubectl is not installed"
CTX="$(kubectl config current-context 2>/dev/null || true)"
if [[ "$CTX" != kind-* && ( -z "${CC_GRADER_CONTEXT:-}" || "$CTX" != "${CC_GRADER_CONTEXT:-}" ) ]]; then
  fail "needs a kind cluster as the current kubectl context, got '${CTX:-none}'"
fi
kubectl get --raw /readyz > "$WORK/readyz.txt" 2>&1 || fail "cluster $CTX is not reachable: $(cat "$WORK/readyz.txt")"
echo "  context $CTX"

echo "▸ unit tests of the reference solution and the starter"
python3 -m unittest discover -s solution/tests > "$WORK/unit.txt" 2>&1 || { cat "$WORK/unit.txt"; fail "solution unit tests"; }
python3 -m unittest discover -s starter/tests > "$WORK/unit-starter.txt" 2>&1 || { cat "$WORK/unit-starter.txt"; fail "starter unit tests"; }
tail -1 "$WORK/unit.txt"

echo "▸ grading solution/ (must earn every point)"
START=$(date +%s)
CODE=0
bash grader/grade.sh solution --json > "$WORK/solution.json" || CODE=$?
python3 - "$WORK/solution.json" <<'PY' || fail "solution/ must score full marks (grade.sh exit $CODE)"
import json, sys
report = json.load(open(sys.argv[1]))
for m in report["milestones"]:
    print(f"  {'PASS' if m['passed'] else 'FAIL'} {m['id']:<11} {m['earned']}/{m['points']} {m['seconds']:>6}s  {m['detail']}")
assert report["passed"] and report["score"] == report["total"], f"score {report['score']}/{report['total']}"
PY
[[ "$CODE" == "0" ]] || fail "grade.sh exited $CODE on solution/"
echo "  solution graded in $(( $(date +%s) - START ))s"

echo "▸ grading starter/ (must score 0 and fail fast)"
START=$(date +%s)
CODE=0
bash grader/grade.sh starter --json > "$WORK/starter.json" || CODE=$?
ELAPSED=$(( $(date +%s) - START ))
[[ "$CODE" == "1" ]] || fail "starter/ should exit 1, got $CODE: $(cat "$WORK/starter.json")"
python3 - "$WORK/starter.json" <<'PY' || fail "starter/ must score 0 with a 'not implemented' detail per milestone"
import json, sys
report = json.load(open(sys.argv[1]))
for m in report["milestones"]:
    print(f"  {'PASS' if m['passed'] else 'FAIL'} {m['id']:<11} {m['seconds']:>6}s  {m['detail'][:90]}")
assert report["score"] == 0 and not report["passed"], f"score {report['score']}"
assert all("not implemented" in m["detail"] for m in report["milestones"]), "a stub was not detected"
PY
(( ELAPSED < 60 )) || fail "grading the starter took ${ELAPSED}s; stubs must fail fast"
echo "  starter graded in ${ELAPSED}s"

echo "▸ no grader namespaces left behind"
DEADLINE=$(( $(date +%s) + 120 ))
while :; do
  kubectl get namespaces -l cloudcaptain.io/grader=copilot -o name > "$WORK/ns.txt"
  [[ -s "$WORK/ns.txt" ]] || break
  (( $(date +%s) < DEADLINE )) || fail "namespaces still present: $(tr '\n' ' ' < "$WORK/ns.txt")"
  sleep 3
done

echo "SMOKE OK: capstones/aiops-copilot"
