#!/usr/bin/env bash
# CI proof that the grader means something: solution/ must pass and starter/ must not.
# Start any real tools the checks need (kind, LocalStack, ...) before grading.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

echo "▸ grading solution/ (must pass)"
bash grader/grade.sh solution || fail "solution/ does not pass the grader"

echo "▸ grading starter/ (must not pass)"
CODE=0
bash grader/grade.sh starter > /dev/null || CODE=$?
[[ "$CODE" == "1" ]] || fail "starter/ should score below the pass mark (exit 1), got exit $CODE"

echo "SMOKE OK: __ID__"
