#!/bin/bash
# usage: watch_checks.sh <pr number>...  -- waits until every check run on each PR head completes, prints the results
SP=/private/tmp/claude-501/-Users-mehul-Documents-CloudCaptain/a4b6cd82-dd89-47d1-bd44-bef474410ede/scratchpad
cd /Users/mehul/Documents/CloudCaptain
for PR in "$@"; do
  SHA=$("$SP/gh_api.sh" GET /repos/nomadicmehul/CloudCaptain/pulls/$PR | python3 -c "import json,sys; print(json.load(sys.stdin)['head']['sha'])")
  for i in $(seq 1 60); do
    OUT=$("$SP/gh_api.sh" GET "/repos/nomadicmehul/CloudCaptain/commits/$SHA/check-runs?per_page=50" | python3 -c "
import json,sys
runs=json.load(sys.stdin)['check_runs']
print('PENDING' if (not runs or any(r['status']!='completed' for r in runs)) else 'DONE')
for r in runs: print('   ', r['name'].ljust(36), r['status'], r['conclusion'])")
    [ "$(echo "$OUT" | head -1)" = "DONE" ] && [ $i -gt 2 ] && break
    sleep 30
  done
  echo "PR #$PR ${SHA:0:7}"; echo "$OUT" | tail -n +2
done
