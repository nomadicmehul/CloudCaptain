#!/usr/bin/env bash
# CI proof that the grader means something: solution/ must earn every point and starter/ none.
# Needs docker, kind, kubectl, git and a kind cluster; lesson-smoke.yml provides kind-cc-smoke.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
BASE_IMAGE="python:3.13-alpine"
WORK="$(mktemp -d)"

leftover_namespaces() {
  kubectl get namespaces -o name > "$WORK/namespaces.txt" 2>/dev/null || true
  sed -n 's#^namespace/\(cc-grade-paas-.*\)$#\1#p' "$WORK/namespaces.txt"
}
leftover_images() {
  docker image ls --format '{{.Repository}}:{{.Tag}}' > "$WORK/images.txt" 2>/dev/null || true
  sed -n 's#^\(docker\.io/\)\{0,1\}\(tinypaas/ccg-.*\)$#\2#p' "$WORK/images.txt"
}
cleanup() {
  set +e
  if [[ -n "${KUBECONFIG:-}" ]]; then
    for ns in $(leftover_namespaces); do kubectl delete namespace "$ns" --wait=false >/dev/null 2>&1; done
  fi
  for image in $(leftover_images); do docker image rm -f "$image" >/dev/null 2>&1; done
  rm -rf "$WORK"
}
trap cleanup EXIT

for tool in docker kind kubectl git python3; do
  command -v "$tool" >/dev/null || fail "needs $tool on PATH"
done

echo "▸ a private kubeconfig whose current context is $CTX (the grader grades the current context)"
kubectl config view --raw > "$WORK/kubeconfig" || fail "cannot read the kubeconfig"
export KUBECONFIG="$WORK/kubeconfig"
kubectl config use-context "$CTX" >/dev/null || fail "no kubectl context $CTX: create the cluster with kind create cluster --name ${CTX#kind-}"
kind get nodes --name "${CTX#kind-}" > "$WORK/nodes.txt" || fail "kind does not know cluster ${CTX#kind-}"
[[ -s "$WORK/nodes.txt" ]] || fail "kind cluster ${CTX#kind-} has no nodes"
kubectl get --raw /readyz > /dev/null || fail "the API server of $CTX is not ready"

echo "▸ the solution's own unit tests"
python3 -m unittest discover -s solution/tests > "$WORK/unit.txt" 2>&1 || fail "solution/tests: $(tail -n 20 "$WORK/unit.txt")"
tail -n 1 "$WORK/unit.txt"

echo "▸ pull the base image once, so the first build is not charged for it"
docker pull -q "$BASE_IMAGE" > /dev/null || fail "docker pull $BASE_IMAGE"

echo "▸ grading solution/ (must earn every point)"
START=$SECONDS
CODE=0
bash grader/grade.sh solution --json > "$WORK/solution.json" 2> "$WORK/solution.err" || CODE=$?
python3 - "$WORK/solution.json" <<'PY' || fail "solution/ must earn every point (grade.sh exit $CODE): $(tail -c 2000 "$WORK/solution.err")"
import json, sys
report = json.load(open(sys.argv[1]))
for m in report["milestones"]:
    print("  %s %-18s %d/%d %6.1fs  %s" % ("PASS" if m["passed"] else "FAIL", m["id"], m["earned"], m["points"], m["seconds"], m["detail"]))
assert report["capstone"] == "capstones/build-a-paas", report["capstone"]
assert not report["partial"] and report["passed"], report["passed"]
assert report["score"] == report["total"] == 15, (report["score"], report["total"])
PY
[[ "$CODE" == "0" ]] || fail "grade.sh solution exited $CODE although every milestone passed"
echo "  solution/ graded 15/15 in $((SECONDS - START))s"

echo "▸ the grader left nothing behind: no cc-grade-paas-* namespaces, no tinypaas/ccg-* images"
[[ -z "$(leftover_namespaces)" ]] || fail "namespaces left behind: $(leftover_namespaces | tr '\n' ' ')"
[[ -z "$(leftover_images)" ]] || fail "images left behind: $(leftover_images | tr '\n' ' ')"

echo "▸ grading starter/ (must score 0, fast)"
START=$SECONDS
CODE=0
bash grader/grade.sh starter --json > "$WORK/starter.json" 2> "$WORK/starter.err" || CODE=$?
ELAPSED=$((SECONDS - START))
[[ "$CODE" == "1" ]] || fail "starter/ should not pass (exit 1), got exit $CODE: $(tail -c 2000 "$WORK/starter.err")"
python3 - "$WORK/starter.json" <<'PY' || fail "starter/ must score 0 with a reason per milestone"
import json, sys
report = json.load(open(sys.argv[1]))
for m in report["milestones"]:
    print("  %s %-18s %s" % ("PASS" if m["passed"] else "FAIL", m["id"], m["detail"]))
assert report["score"] == 0 and not report["passed"], report["score"]
assert all("not implemented: milestone" in m["detail"] for m in report["milestones"]), [m["detail"] for m in report["milestones"]]
PY
(( ELAPSED < 60 )) || fail "grading starter/ took ${ELAPSED}s; stubs must fail in under a minute"
echo "  starter/ scored 0/15 in ${ELAPSED}s"

echo "SMOKE OK: capstones/build-a-paas"
