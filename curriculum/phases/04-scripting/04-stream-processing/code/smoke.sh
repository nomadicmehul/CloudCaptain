#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux"
command -v jq >/dev/null || fail "needs jq (preinstalled on ubuntu-latest)"
command -v awk >/dev/null || fail "needs awk"
command -v python3 >/dev/null || fail "needs python3"
WORK="$(mktemp -d)"
cleanup() {
  rm -rf "$WORK"
}
trap cleanup EXIT
MS="$PWD/code/main.py"

{ awk -W version 2>/dev/null || awk --version 2>/dev/null || echo "unknown awk"; } > "$WORK/awk-version.txt"
echo "▸ tools: $(jq --version), awk: $(head -n 1 "$WORK/awk-version.txt"), $(python3 --version)"

cat > "$WORK/generate.py" <<'PY'
import json, random, sys
count, jsonl_path, csv_path, log_path = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
rng = random.Random(42)
paths = ["/", "/api/users", "/api/orders", "/api/orders/checkout", "/login", "/static/app.js", "/health"]
statuses = [200] * 12 + [201, 204, 301, 304, 400, 401, 403, 404, 404, 500, 502, 503]
users = [None, "alice", "bob", "carol", "dave"]
hosts = ["10.0.1.11:8080", "10.0.1.12:8080", "10.0.2.21:9000"]
with open(jsonl_path, "w") as jsonl, open(csv_path, "w") as csv, open(log_path, "w") as log:
    csv.write("ts,method,path,status,bytes,ms,user\n")
    for index in range(count):
        seconds = index * 7
        ts = "2026-11-10T%02d:%02d:%02dZ" % (seconds // 3600 % 24, seconds // 60 % 60, seconds % 60)
        record = {
            "ts": ts,
            "method": rng.choice(["GET", "GET", "GET", "GET", "POST", "PUT", "DELETE"]),
            "path": rng.choice(paths),
            "status": rng.choice(statuses),
            "bytes": rng.randint(0, 60000),
            "ms": round(rng.lognormvariate(3.5, 1.0), 3),
            "user": rng.choice(users),
            "tags": rng.sample(["canary", "mobile", "eu", "us", "bot"], rng.randint(0, 2)),
            "upstream": {"host": rng.choice(hosts), "retries": rng.randint(0, 2)},
        }
        if index % 97 == 5:
            del record["user"]
        jsonl.write(json.dumps(record, separators=(",", ":")) + "\n")
        csv.write("%s,%s,%s,%d,%d,%s,%s\n" % (ts, record["method"], record["path"], record["status"], record["bytes"], record["ms"], record.get("user") or ""))
        log.write('10.1.%d.%d - %s [%s] "%s %s HTTP/1.1" %d %d %s\n' % (index % 7, index % 13, record.get("user") or "-", ts, record["method"], record["path"], record["status"], record["bytes"], record["ms"]))
PY
python3 "$WORK/generate.py" 3000 "$WORK/access.jsonl" "$WORK/access.csv" "$WORK/access.log"
echo "  generated $(wc -l < "$WORK/access.jsonl") JSON Lines records, a CSV and a space-separated log"

cat > "$WORK/same_json.py" <<'PY'
import json, sys
def values(path):
    text = open(path, encoding="utf-8").read()
    decoder, position, out = json.JSONDecoder(), 0, []
    while True:
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            return out
        value, position = decoder.raw_decode(text, position)
        out.append(value)
expected, actual = values(sys.argv[1]), values(sys.argv[2])
if expected != actual:
    for index, (want, got) in enumerate(zip(expected, actual)):
        if want != got:
            print(f"first difference at output {index}: jq={json.dumps(want)[:200]} ministream={json.dumps(got)[:200]}")
            break
    print(f"jq produced {len(expected)} values, ministream {len(actual)}")
    sys.exit(1)
print(len(expected))
PY

cat > "$WORK/jq-cases.txt" <<'EOF'
-c	.
-c	.path
-c	.upstream.host
-c	.tags[0]
-c	.tags[]
-c	.missing.deeper
-c	select(.status >= 500) | {ts, path, status}
-c	select(.method == "POST" and .status < 300) | .bytes
-c	{path, slow: (.ms > 100)}
-c	[.status, .bytes] | add
-c	.bytes / 1024 | floor
-c	.user // "anonymous"
-c	if .status >= 500 then "server" elif .status >= 400 then "client" else "ok" end
-c	keys
-c	has("user")
-c	.upstream | to_entries
-c	length
-c	.tags | length
-c	.path | split("/") | map(select(length > 0))
-c	.upstream | with_entries(select(.key == "host"))
-c	{(.method): .status}
-c	.status as $s | .tags | map("\($s)-\(.)")
-c	.path | test("^/api/")
-c	.path | ascii_upcase | ltrimstr("/")
-c	.path | .[1:4]
-c	.tags | first
-r	.path
-r	"\(.method) \(.path) \(.status)"
-r	[.method, .path, .user] | @tsv
-c -s	length
-c -s	group_by(.status) | map({status: .[0].status, count: length})
-c -s	map(.bytes) | add
-c -s	map(.ms) | add / length
-c -s	sort_by(.ms) | .[-3:] | map(.ms)
-c -s	group_by(.path) | map({path: .[0].path, slowest: (map(.ms) | max), n: length}) | sort_by(-.n)
-c -s	map(select(.user != null)) | group_by(.user) | map({(.[0].user): length}) | add
-c -s	[.[] | .tags[]] | unique
-c -s	reduce .[] as $r (0; . + $r.bytes)
-c -s	min_by(.ms) | .ts
-c -s	map(.status) | unique | map(tostring) | join(",")
-c -s	map(.status == 503) | any
-c -s	[limit(3; .[] | select(.status == 404) | .path)]
-r -s	.[:5][] | [.ts, .method, .status] | @csv
-c -n	[range(5)] | map(. * .)
EOF

echo "▸ jq: $(wc -l < "$WORK/jq-cases.txt") programs through ministream and the real jq, outputs parsed and compared"
case_number=0
while IFS=$'\t' read -r flags program; do
  case_number=$((case_number + 1))
  read -r -a flag_list <<< "$flags"
  jq "${flag_list[@]}" "$program" "$WORK/access.jsonl" > "$WORK/jq.out" 2> "$WORK/jq.err" || fail "jq failed on case $case_number ($flags '$program'): $(head -c 300 "$WORK/jq.err")"
  python3 "$MS" jq "${flag_list[@]}" "$program" "$WORK/access.jsonl" > "$WORK/ms.out" 2> "$WORK/ms.err" || fail "ministream failed on case $case_number ($flags '$program'): $(head -c 300 "$WORK/ms.err")"
  if [[ " $flags " == *" -r "* ]]; then
    cmp -s "$WORK/jq.out" "$WORK/ms.out" || fail "case $case_number ($flags '$program'): raw output differs: $(diff "$WORK/jq.out" "$WORK/ms.out" | head -c 400)"
    count="$(wc -l < "$WORK/jq.out")"
  else
    count="$(python3 "$WORK/same_json.py" "$WORK/jq.out" "$WORK/ms.out")" || fail "case $case_number ($flags '$program'): $count"
  fi
  printf '  %2d ok  %-6s %-70s %s outputs\n' "$case_number" "$flags" "$program" "$count"
done < "$WORK/jq-cases.txt"
[[ "$case_number" -ge 25 ]] || fail "expected at least 25 jq cases, ran $case_number"

cat > "$WORK/awk-cases.txt" <<'EOF'
-F,	plain	{ print $3 }
-F,	plain	NR > 1 && $4 >= 500 { print $1, $3, $4 }
-F,	plain	NR > 1 { bytes += $5 } END { print bytes, NR - 1 }
-F,	sorted	NR > 1 { n[$4]++ } END { for (s in n) print s, n[s] }
-F,	sorted	NR > 1 { sum[$3] += $6; cnt[$3]++ } END { for (p in sum) printf "%s %.3f %d\n", p, sum[p] / cnt[p], cnt[p] }
-F,	plain	$2 ~ /^(POST|PUT)$/ { c++ } END { print c + 0 }
-F,	plain	BEGIN { OFS = "\t" } NR > 1 { print NR, toupper($2), length($3) }
-F,	plain	NR > 1 && $7 == "" { anon++ } END { printf "anonymous=%d of %d\n", anon, NR - 1 }
-F,	sorted	NR > 1 { split($1, t, "T"); split(t[2], hms, ":"); per[hms[1] ":" hms[2]]++ } END { for (m in per) print m, per[m] }
-F,	plain	NR > 1 && $6 > max { max = $6; at = $1 } END { print max, at }
-F,	plain	NR > 1 { $5 = int($5 / 1024) "k"; print }
-F,	plain	NR == 1 { next } { gsub(/\//, "_", $3); print $3, $0 }
-F,	plain	NR > 1 { print substr($1, 12, 5), index($3, "api"), ($4 >= 400 ? "bad" : "good") }
-F,	plain	NR > 1 && match($3, /[a-z]+/) { print RSTART, RLENGTH, substr($3, RSTART, RLENGTH) }
-F,	plain	NR > 1 { printf "%-8s|%6d|%8.2f|%5s|%x\n", $2, $5, $6, $4, $4 }
-F,	plain	NR > 1 && !seen[$3]++ { print $3 }
-F,	plain	NR > 1 { if ($4 < 300) ok++; else if ($4 < 500) client++; else server++ } END { print ok, client, server }
-F,	plain	NR <= 4 { n = split($0, parts, ","); for (i = n; i > 0; i--) printf "%s%s", parts[i], (i > 1 ? ";" : "\n") }
-F,	plain	END { print NR, $1 }
-	plain	{ print $1, $8 }
-	plain	$8 >= 500 { print $6, $8 }
-	sorted	{ hits[$1]++; if ($3 != "-") users[$3] = 1 } END { for (ip in hits) print ip, hits[ip]; print "users", length(users) }
-	plain	{ total += $NF; if ($NF > 100) slow++ } END { printf "%.3f %d %.1f%%\n", total / NR, slow, 100 * slow / NR }
EOF

echo "▸ awk: $(wc -l < "$WORK/awk-cases.txt") programs through ministream and the runner's awk, text compared byte for byte"
case_number=0
while IFS=$'\t' read -r separator order program; do
  case_number=$((case_number + 1))
  input="$WORK/access.csv"
  separator_args=(-F ,)
  if [[ "$separator" == "-" ]]; then
    input="$WORK/access.log"
    separator_args=()
  fi
  awk "${separator_args[@]}" "$program" "$input" > "$WORK/awk.out" 2> "$WORK/awk.err" || fail "awk failed on case $case_number ('$program'): $(head -c 300 "$WORK/awk.err")"
  python3 "$MS" awk "${separator_args[@]}" "$program" "$input" > "$WORK/ms.out" 2> "$WORK/ms.err" || fail "ministream failed on case $case_number ('$program'): $(head -c 300 "$WORK/ms.err")"
  if [[ "$order" == "sorted" ]]; then
    LC_ALL=C sort "$WORK/awk.out" > "$WORK/awk.sorted" && mv "$WORK/awk.sorted" "$WORK/awk.out"
    LC_ALL=C sort "$WORK/ms.out" > "$WORK/ms.sorted" && mv "$WORK/ms.sorted" "$WORK/ms.out"
  fi
  cmp -s "$WORK/awk.out" "$WORK/ms.out" || fail "awk case $case_number ('$program'): output differs: $(diff "$WORK/awk.out" "$WORK/ms.out" | head -c 400)"
  printf '  %2d ok  %-6s %-90.90s %s lines\n' "$case_number" "$order" "$program" "$(wc -l < "$WORK/awk.out")"
done < "$WORK/awk-cases.txt"
[[ "$case_number" -ge 10 ]] || fail "expected at least 10 awk cases, ran $case_number"

echo "▸ awk -v passes a value in before BEGIN, in both"
awk -F, -v limit=250 'NR > 1 && $6 > limit { slow++ } END { print slow + 0 }' "$WORK/access.csv" > "$WORK/awk.out"
python3 "$MS" awk -F, -v limit=250 'NR > 1 && $6 > limit { slow++ } END { print slow + 0 }' "$WORK/access.csv" > "$WORK/ms.out"
cmp -s "$WORK/awk.out" "$WORK/ms.out" || fail "-v limit: awk said $(cat "$WORK/awk.out"), ministream $(cat "$WORK/ms.out")"
echo "  $(cat "$WORK/ms.out") requests slower than 250 ms"

echo "▸ errors: a syntax error exits non-zero in both, ministream with 2 and a line and column"
set +e
jq '.path |' "$WORK/access.jsonl" > /dev/null 2> "$WORK/jq.err"; jq_code=$?
python3 "$MS" jq '.path |' "$WORK/access.jsonl" > /dev/null 2> "$WORK/ms.err"; ms_code=$?
set -e
[[ "$jq_code" -ne 0 ]] || fail "jq should reject '.path |'"
[[ "$ms_code" -eq 2 ]] || fail "ministream should exit 2 on a syntax error, got $ms_code: $(cat "$WORK/ms.err")"
grep -q "syntax error at line 1, column" "$WORK/ms.err" || fail "expected a positioned syntax error, got: $(cat "$WORK/ms.err")"
echo "  jq exit $jq_code: $(head -n 1 "$WORK/jq.err")"
echo "  ministream exit $ms_code: $(head -n 1 "$WORK/ms.err")"

echo "▸ errors: a runtime error on some records is reported per record and the stream goes on"
program='if .status == 503 then .status.x else .path end'
set +e
jq -c "$program" "$WORK/access.jsonl" > "$WORK/jq.out" 2> "$WORK/jq.err"; jq_code=$?
python3 "$MS" jq -c "$program" "$WORK/access.jsonl" > "$WORK/ms.out" 2> "$WORK/ms.err"; ms_code=$?
set -e
[[ "$ms_code" -eq 1 ]] || fail "ministream should exit 1 when any record failed, got $ms_code"
[[ "$jq_code" -eq 0 || "$jq_code" -eq 5 ]] || fail "jq should exit 0 or 5, got $jq_code: $(head -c 300 "$WORK/jq.err")"
python3 "$WORK/same_json.py" "$WORK/jq.out" "$WORK/ms.out" > /dev/null || fail "the records without errors should produce the same output"
jq_errors="$(grep -c 'Cannot index number with' "$WORK/jq.err" || true)"
ms_errors="$(grep -c 'Cannot index number with' "$WORK/ms.err" || true)"
[[ "$jq_errors" -gt 0 && "$jq_errors" == "$ms_errors" ]] || fail "expected the same number of per-record errors, jq $jq_errors, ministream $ms_errors"
echo "  $(wc -l < "$WORK/ms.out") outputs and $ms_errors errors in both: $(head -n 1 "$WORK/jq.err")"
echo "  ministream exit $ms_code (some record failed); jq exit $jq_code (jq's status reflects only the last input, which was fine)"

echo "▸ errors: invalid JSON in the middle of the input stops both after the same good values"
{ head -n 3 "$WORK/access.jsonl"; echo '{"path": "/broken",'; head -n 2 "$WORK/access.jsonl"; } > "$WORK/broken.jsonl"
set +e
jq -c .path "$WORK/broken.jsonl" > "$WORK/jq.out" 2> "$WORK/jq.err"; jq_code=$?
python3 "$MS" jq -c .path "$WORK/broken.jsonl" > "$WORK/ms.out" 2> "$WORK/ms.err"; ms_code=$?
set -e
[[ "$jq_code" -ne 0 && "$ms_code" -ne 0 ]] || fail "both should fail on invalid input: jq $jq_code, ministream $ms_code"
python3 "$WORK/same_json.py" "$WORK/jq.out" "$WORK/ms.out" > /dev/null || fail "both should print the values before the bad one: jq $(cat "$WORK/jq.out"), ministream $(cat "$WORK/ms.out")"
echo "  both printed $(wc -l < "$WORK/ms.out") values, then: $(head -n 1 "$WORK/ms.err")"

echo "▸ explain prints the parsed program"
python3 "$MS" explain jq 'select(.status >= 500) | {path}' > "$WORK/explain.txt"
grep -q '^pipe$' "$WORK/explain.txt" && grep -q 'call "select"' "$WORK/explain.txt" && grep -q 'compare ">="' "$WORK/explain.txt" || fail "explain jq: $(cat "$WORK/explain.txt")"
python3 "$MS" explain --json awk '$4 >= 500 { n[$3]++ }' > "$WORK/explain.json"
python3 -c 'import json,sys; t=json.load(open(sys.argv[1])); assert t["node"]=="program", t; assert t["args"][0][0]["node"]=="rule", t' "$WORK/explain.json" || fail "explain --json awk: $(cat "$WORK/explain.json")"
sed 's/^/  /' "$WORK/explain.txt"

echo "▸ Use It: sort | uniq -c | sort -rn gives the same top paths as ministream's group_by"
jq -r '.path' "$WORK/access.jsonl" | sort | uniq -c | sort -rn > "$WORK/pipeline.txt"
python3 "$MS" jq -r -s 'group_by(.path) | map({path: .[0].path, n: length}) | sort_by(-.n, .path)[] | "\(.n) \(.path)"' "$WORK/access.jsonl" > "$WORK/ms-top.txt"
awk '{ print $1, $2 }' "$WORK/pipeline.txt" | sort -k1,1nr -k2,2 > "$WORK/pipeline-normalized.txt"
cmp -s "$WORK/pipeline-normalized.txt" "$WORK/ms-top.txt" || fail "top paths differ: $(diff "$WORK/pipeline-normalized.txt" "$WORK/ms-top.txt" | head -c 400)"
head -n 3 "$WORK/pipeline.txt" | sed 's/^/  /'

echo "▸ Use It: sed and ministream awk agree on a CSV to TSV conversion"
sed 's/,/\t/g' "$WORK/access.csv" > "$WORK/sed.tsv"
python3 "$MS" awk -F, 'BEGIN { OFS = "\t" } { $1 = $1; print }' "$WORK/access.csv" > "$WORK/ms.tsv"
cmp -s "$WORK/sed.tsv" "$WORK/ms.tsv" || fail "sed and the \$1 = \$1 rebuild idiom differ: $(diff "$WORK/sed.tsv" "$WORK/ms.tsv" | head -c 300)"

echo "▸ Use It: xargs -P runs ministream on four chunks at once; the merged counts equal one pass"
python3 "$WORK/generate.py" 40000 "$WORK/big.jsonl" "$WORK/big.csv" "$WORK/big.log"
mkdir -p "$WORK/parts"
split -l 10000 "$WORK/big.jsonl" "$WORK/parts/chunk-"
find "$WORK/parts" -name 'chunk-*' -print0 | xargs -0 -P 4 -I{} sh -c 'python3 "$1" jq -r "select(.status >= 500) | .path" "$2" > "$2.paths"' _ "$MS" {}
cat "$WORK"/parts/*.paths | sort | uniq -c | sort -k1,1nr -k2,2 > "$WORK/parallel.txt"
jq -r 'select(.status >= 500) | .path' "$WORK/big.jsonl" | sort | uniq -c | sort -k1,1nr -k2,2 > "$WORK/single.txt"
cmp -s "$WORK/parallel.txt" "$WORK/single.txt" || fail "map-reduce over chunks differs from one pass: $(diff "$WORK/parallel.txt" "$WORK/single.txt" | head -c 300)"
echo "  $(ls "$WORK"/parts/*.paths | wc -l) chunks, $(awk '{ s += $1 } END { print s }' "$WORK/parallel.txt") server errors, same counts as a single jq pass"
if command -v parallel >/dev/null; then
  find "$WORK/parts" -name 'chunk-??' | parallel -j 4 "python3 '$MS' jq -r 'select(.status >= 500) | .path' {}" | sort | uniq -c | sort -k1,1nr -k2,2 > "$WORK/gnu-parallel.txt"
  cmp -s "$WORK/gnu-parallel.txt" "$WORK/single.txt" || fail "GNU parallel result differs"
  echo "  GNU parallel: same counts"
else
  echo "  (GNU parallel not installed; parallel comparison skipped)"
fi

if command -v yq >/dev/null; then
  echo "▸ Use It: yq turns YAML into JSON that ministream and jq read the same way"
  printf 'services:\n  - name: api\n    replicas: 3\n    ports: [8080, 8443]\n  - name: worker\n    replicas: 2\n    ports: []\n' > "$WORK/services.yaml"
  if yq -o=json '.' "$WORK/services.yaml" > "$WORK/services.json" 2> "$WORK/yq.err"; then
    jq -c '.services | map({name, replicas, public: (.ports | length > 0)})' "$WORK/services.json" > "$WORK/jq.out"
    python3 "$MS" jq -c '.services | map({name, replicas, public: (.ports | length > 0)})' "$WORK/services.json" > "$WORK/ms.out"
    python3 "$WORK/same_json.py" "$WORK/jq.out" "$WORK/ms.out" > /dev/null || fail "yq output read differently: jq $(cat "$WORK/jq.out"), ministream $(cat "$WORK/ms.out")"
    echo "  $(cat "$WORK/ms.out")"
  else
    echo "  (this yq is not mikefarah/yq: $(head -c 200 "$WORK/yq.err"); skipped)"
  fi
else
  echo "  (yq not installed; YAML step skipped)"
fi

cat > "$WORK/peak_rss.py" <<'PY'
import resource, subprocess, sys
with open(sys.argv[1], "w") as sink:
    subprocess.run(sys.argv[2:], stdout=sink, check=True)
print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
PY
echo "▸ constant memory: peak RSS for 10x more input stays flat when streaming and grows with -s"
python3 "$WORK/generate.py" 8000 "$WORK/small.jsonl" "$WORK/small.csv" "$WORK/small.log"
python3 "$WORK/generate.py" 120000 "$WORK/large.jsonl" "$WORK/large.csv" "$WORK/large.log"
small_mb=$(( $(wc -c < "$WORK/small.jsonl") / 1048576 )); large_mb=$(( $(wc -c < "$WORK/large.jsonl") / 1048576 ))
filter='select(.status >= 500) | .path'
ms_small="$(python3 "$WORK/peak_rss.py" "$WORK/rss.out" python3 "$MS" jq -r "$filter" "$WORK/small.jsonl")"
ms_large="$(python3 "$WORK/peak_rss.py" "$WORK/rss.out" python3 "$MS" jq -r "$filter" "$WORK/large.jsonl")"
ms_slurp="$(python3 "$WORK/peak_rss.py" "$WORK/rss.out" python3 "$MS" jq -r -s ".[] | $filter" "$WORK/large.jsonl")"
jq_small="$(python3 "$WORK/peak_rss.py" "$WORK/rss.out" jq -r "$filter" "$WORK/small.jsonl")"
jq_large="$(python3 "$WORK/peak_rss.py" "$WORK/rss.out" jq -r "$filter" "$WORK/large.jsonl")"
jq_slurp="$(python3 "$WORK/peak_rss.py" "$WORK/rss.out" jq -r -s ".[] | $filter" "$WORK/large.jsonl")"
awk_small="$(python3 "$WORK/peak_rss.py" "$WORK/rss.out" python3 "$MS" awk -F, '{ b += $5 } END { print b }' "$WORK/small.csv")"
awk_large="$(python3 "$WORK/peak_rss.py" "$WORK/rss.out" python3 "$MS" awk -F, '{ b += $5 } END { print b }' "$WORK/large.csv")"
printf '  input %s MB vs %s MB (KB peak RSS)\n' "$small_mb" "$large_mb"
printf '  ministream jq: %s -> %s, with -s %s\n' "$ms_small" "$ms_large" "$ms_slurp"
printf '  jq:            %s -> %s, with -s %s\n' "$jq_small" "$jq_large" "$jq_slurp"
printf '  ministream awk: %s -> %s\n' "$awk_small" "$awk_large"
input_growth_kb=$(( ($(wc -c < "$WORK/large.jsonl") - $(wc -c < "$WORK/small.jsonl")) / 1024 ))
[[ $(( ms_large - ms_small )) -lt 8192 ]] || fail "ministream jq peak RSS grew by $(( ms_large - ms_small )) KB for $input_growth_kb KB more input"
[[ $(( awk_large - awk_small )) -lt 8192 ]] || fail "ministream awk peak RSS grew by $(( awk_large - awk_small )) KB"
[[ $(( ms_slurp - ms_small )) -gt "$input_growth_kb" ]] || fail "with -s ministream should hold the whole input: $ms_slurp KB vs $ms_small KB"
[[ $(( jq_large - jq_small )) -lt 8192 ]] || fail "jq peak RSS grew by $(( jq_large - jq_small )) KB while streaming"
[[ $(( jq_slurp - jq_small )) -gt "$input_growth_kb" ]] || fail "jq -s should hold the whole input: $jq_slurp KB vs $jq_small KB"

echo "SMOKE OK: scripting/stream-processing"
