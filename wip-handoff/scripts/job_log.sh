#!/bin/bash
# usage: job_log.sh <head-sha> <job-name-substring> <out-file>   -- downloads the log of one job of the Lesson Smoke run for that sha
set -euo pipefail
SHA="$1"; NAME="$2"; OUT="$3"
D="$(cd "$(dirname "$0")" && pwd)"
RUN=$(bash "$D/gh_api.sh" GET "/repos/nomadicmehul/CloudCaptain/actions/runs?head_sha=$SHA&per_page=50" | python3 -c '
import json,sys
runs=[r for r in json.load(sys.stdin)["workflow_runs"] if r["name"].startswith("Lesson Smoke")]
print(runs[0]["id"] if runs else "")')
[ -n "$RUN" ] || { echo "no Lesson Smoke run for $SHA" >&2; exit 1; }
JOB=$(bash "$D/gh_api.sh" GET "/repos/nomadicmehul/CloudCaptain/actions/runs/$RUN/jobs?per_page=50" | python3 -c '
import json,sys
name=sys.argv[1]
jobs=[j for j in json.load(sys.stdin)["jobs"] if name in j["name"]]
print(jobs[0]["id"], jobs[0]["conclusion"]) if jobs else print("")' "$NAME")
[ -n "$JOB" ] || { echo "no job matching $NAME" >&2; exit 1; }
set -- $JOB
TOKEN=$(printf 'protocol=https\nhost=github.com\n' | git credential fill | awk -F= '$1=="password"{print $2}')
curl -sSL -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" "https://api.github.com/repos/nomadicmehul/CloudCaptain/actions/jobs/$1/logs" -o "$OUT"
echo "job $1 conclusion=$2 -> $OUT ($(wc -l < "$OUT") lines)"
