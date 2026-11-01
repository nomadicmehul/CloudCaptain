#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
F="code/fixtures"
VENV="$WORK/venv"
MOTO_PID=""
cleanup() {
  if [[ -n "$MOTO_PID" ]]; then
    kill "$MOTO_PID" >/dev/null 2>&1 || true
    wait "$MOTO_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

exit_of() {
  local out="$1"
  shift
  local code=0
  "$@" > "$out" 2> "$out.err" || code=$?
  echo "$code"
}
check() {
  local what="$1"
  shift
  python3 - "$@" > "$WORK/check.txt" 2>&1 || fail "$what: $(cat "$WORK/check.txt")"
  sed 's/^/  /' "$WORK/check.txt"
}

echo "▸ billjoin on the FOCUS 1.4 specification's own examples (Flexible Spend Plan at 75%, Resource Reservation partial upfront)"
CODE="$(exit_of "$WORK/fsp.json" python3 code/main.py report "$F/focus-spec-flexible-spend-plan-75pct.csv" --json)"
[[ "$CODE" == "0" ]] || fail "spec example report exit $CODE: $(cat "$WORK/fsp.json" "$WORK/fsp.json.err")"
CODE="$(exit_of "$WORK/rr.json" python3 code/main.py report "$F/focus-spec-reservation-partial-upfront.csv" --json)"
[[ "$CODE" == "0" ]] || fail "spec example report exit $CODE: $(cat "$WORK/rr.json" "$WORK/rr.json.err")"
check "spec examples" "$WORK/fsp.json" "$WORK/rr.json" <<'PY'
import json, sys
fsp, rr = (json.load(open(p)) for p in sys.argv[1:3])
plan, reservation = fsp["commitments"][0], rr["commitments"][0]
assert fsp["totals"]["billed"] == "459024.00" and fsp["totals"]["effective"] == "1257.60", fsp["totals"]
assert (plan["utilisation"], plan["unused_effective"], plan["amortisation_gap"]) == ("75.00", "314.40", "0.00"), plan
assert fsp["coverage"][0]["esr"] == "11.11", fsp["coverage"]
assert (reservation["expected_effective"], reservation["amortisation_gap"]) == ("1205.52", "0.00"), reservation
print("Flexible Spend Plan: 18 of 24 hours used = 75.00%, waste 314.40, 459024.00 / 8760 h = 52.40 per hour, gap 0.00")
print("Resource Reservation: 220007.40 / 8760 h + 16877.28 / 672 h = 50.23 per hour x 24 h = 1205.52")
PY

echo "▸ the month: showback by team with the tag policy, totals reconcile to the cent, coverage and utilisation match the hand calculation"
CODE="$(exit_of "$WORK/month.json" python3 code/main.py report "$F/focus-month.csv" --tag-policy "$F/tag-policy.json" --units "$F/units.json" --invoice INV-2026-10-0001=1077.28 --json)"
[[ "$CODE" == "0" ]] || fail "month report exit $CODE: $(cat "$WORK/month.json" "$WORK/month.json.err")"
CODE="$(exit_of "$WORK/spread.json" python3 code/main.py report "$F/focus-month.csv" --tag-policy "$F/tag-policy.json" --spread-shared --json)"
[[ "$CODE" == "0" ]] || fail "spread report exit $CODE: $(cat "$WORK/spread.json.err")"
check "month allocation" "$F/focus-month.csv" "$WORK/month.json" "$WORK/spread.json" <<'PY'
import csv, json, sys
from decimal import Decimal as D
rows = list(csv.DictReader(open(sys.argv[1])))
month, spread = json.load(open(sys.argv[2])), json.load(open(sys.argv[3]))
effective = sum(D(r["EffectiveCost"]) for r in rows)
billed = sum(D(r["BilledCost"]) for r in rows)
lines = {l["name"]: D(l["effective"]) for l in month["allocation"]["lines"]}
want = {"payments": D("403.20"), "platform": D("369.60"), "search": D("252.00"), "(untagged)": D("100.80"), "(unused commitment)": D("100.80"), "(shared)": D("52.48")}
assert lines == want, lines
for doc in (month, spread):
    assert sum(D(l["effective"]) for l in doc["allocation"]["lines"]) == effective == D(doc["allocation"]["total"]), doc["allocation"]
assert D(month["totals"]["billed"]) == billed == D("1077.28"), month["totals"]
assert month["invoices"][0]["ok"] and month["invoices"][0]["tolerance"] == "1.00", month["invoices"]
ri = next(c for c in month["commitments"] if c["type"] == "Reserved Instance")
assert ri["utilisation"] == str((D(100) * 1008 / (1008 + 336)).quantize(D("0.01"))) == "75.00", ri
assert ri["amortisation_gap"] == "201.60", ri
cover = {c["service_category"]: c["coverage"] for c in month["coverage"]}
assert cover["Compute"] == str((D(100) * 48 / (48 + 12)).quantize(D("0.01"))) == "80.00", cover
assert cover["Databases"] == "100.00", cover
units = {u["line"]: u["unit_cost"] for u in month["unit_costs"]}
assert units["payments"] == "0.48", units
print("lines sum to %s = sum of EffectiveCost over %d rows; BilledCost %s = invoice; RI 1008/1344 h = 75.00%%; Compute coverage 48/60 = 80.00%%" % (effective, len(rows), billed))
print("with --spread-shared: " + ", ".join("%s %s" % (l["name"], l["effective"]) for l in spread["allocation"]["lines"]))
PY
CODE="$(exit_of "$WORK/mismatch.txt" python3 code/main.py report "$F/focus-month.csv" --invoice INV-2026-10-0001=1080.00)"
[[ "$CODE" == "1" ]] && grep -q "MISMATCH" "$WORK/mismatch.txt" || fail "an invoice 2.72 away (tolerance 1.00) should fail with exit 1, got $CODE: $(cat "$WORK/mismatch.txt")"

echo "▸ map-cur: the CUR 2.0 style export becomes the same FOCUS rows; BilledCost equals line_item_unblended_cost to the cent"
CODE="$(exit_of "$WORK/map.txt" python3 code/main.py map-cur "$F/cur-month.csv" -o "$WORK/cur-focus.csv")"
[[ "$CODE" == "0" ]] || fail "map-cur exit $CODE: $(cat "$WORK/map.txt.err")"
cat "$WORK/map.txt.err"
CODE="$(exit_of "$WORK/cur.json" python3 code/main.py report "$WORK/cur-focus.csv" --tag-policy "$F/tag-policy.json" --json)"
[[ "$CODE" == "0" ]] || fail "report on the mapped CUR exit $CODE: $(cat "$WORK/cur.json" "$WORK/cur.json.err")"
check "CUR mapping" "$F/cur-month.csv" "$WORK/cur.json" "$WORK/month.json" <<'PY'
import csv, json, sys
from decimal import Decimal as D
cur = list(csv.DictReader(open(sys.argv[1])))
mapped, month = json.load(open(sys.argv[2])), json.load(open(sys.argv[3]))
unblended = sum(D(r["line_item_unblended_cost"]) for r in cur)
assert D(mapped["totals"]["billed"]) == unblended, (mapped["totals"], unblended)
assert mapped["allocation"]["lines"] == month["allocation"]["lines"], (mapped["allocation"]["lines"], month["allocation"]["lines"])
assert mapped["commitments"] == month["commitments"] and mapped["coverage"] == month["coverage"]
types = sorted({r["line_item_line_item_type"] for r in cur})
print("%d CUR lines (%s) -> same showback, commitments and coverage as the FOCUS fixture" % (len(cur), ", ".join(types)))
PY

echo "▸ anomalies: the clean month has none; an injected re-index job on search is flagged with its cause"
CODE="$(exit_of "$WORK/clean.txt" python3 code/main.py anomalies "$F/focus-month.csv" --tag-policy "$F/tag-policy.json")"
[[ "$CODE" == "0" ]] && grep -q "none" "$WORK/clean.txt" || fail "clean month should have no anomalies (exit $CODE): $(cat "$WORK/clean.txt")"
python3 - "$F/focus-month.csv" "$WORK/spike.csv" <<'PY'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
template = next(r for r in rows if r["ResourceId"] == "i-0search01" and r["ChargePeriodStart"].startswith("2026-10-12"))
spike = dict(template, ResourceId="i-0search-reindex", ConsumedQuantity="96", ListCost="96.00", BilledCost="96.00", EffectiveCost="96.00")
with open(sys.argv[2], "w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows + [spike])
PY
CODE="$(exit_of "$WORK/spike.json" python3 code/main.py anomalies "$WORK/spike.csv" --tag-policy "$F/tag-policy.json" --json)"
[[ "$CODE" == "1" ]] || fail "the spike should exit 1, got $CODE: $(cat "$WORK/spike.json" "$WORK/spike.json.err")"
check "anomaly" "$WORK/spike.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
[a] = doc["anomalies"]
assert (a["group"], a["day"], a["baseline"], a["impact"]) == ("search", "2026-10-12", "18.00", "96.00"), a
assert a["contributors"][0] == {"source": "i-0search-reindex", "delta": "96.00"}, a
print("%s %s: %s against a 7-day median of %s, cause %s" % (a["day"], a["group"], a["actual"], a["baseline"], a["contributors"][0]["source"]))
PY

echo "▸ installing the Use It tools into a venv: DuckDB (SQL over FOCUS, as Athena or BigQuery would run it) and moto (AWS APIs)"
python3 -m venv "$VENV" || fail "python3 -m venv failed"
"$VENV/bin/pip" install --quiet --disable-pip-version-check 'duckdb==1.4.5' 'moto[server]==5.1.22' > "$WORK/pip.log" 2>&1 || fail "pip install: $(tail -20 "$WORK/pip.log")"
if command -v aws >/dev/null; then
  AWS_BIN="$(command -v aws)"
else
  echo "  (aws CLI not installed; installing awscli from PyPI into its own venv, so its botocore pin cannot break moto's boto3)"
  python3 -m venv "$WORK/awscli" || fail "python3 -m venv failed"
  "$WORK/awscli/bin/pip" install --quiet --disable-pip-version-check 'awscli==1.44.87' > "$WORK/pip-aws.log" 2>&1 || fail "pip install awscli: $(tail -20 "$WORK/pip-aws.log")"
  AWS_BIN="$WORK/awscli/bin/aws"
fi
"$AWS_BIN" --version

echo "▸ DuckDB: the same showback, utilisation and coverage in SQL over the FOCUS file agree with billjoin to the cent"
"$VENV/bin/python" - "$F/focus-month.csv" "$WORK/month.json" > "$WORK/duck.txt" 2>&1 <<'PY' || fail "duckdb: $(cat "$WORK/duck.txt")"
import json, sys
from decimal import Decimal as D
import duckdb
path, billjoin = sys.argv[1], json.load(open(sys.argv[2]))
con = duckdb.connect()
con.execute("CREATE VIEW focus AS SELECT * FROM read_csv('%s', header = true, all_varchar = true)" % path.replace("'", "''"))
showback = dict(con.execute("""
    SELECT CASE
             WHEN json_extract_string(Tags, '$.team') IN ('payments', 'search', 'platform') THEN json_extract_string(Tags, '$.team')
             WHEN json_extract_string(Tags, '$.team') IS NOT NULL THEN '(untagged)'
             WHEN CommitmentDiscountStatus = 'Unused' THEN '(unused commitment)'
             WHEN ChargeCategory IN ('Tax', 'Credit', 'Adjustment') THEN '(shared)'
             ELSE '(untagged)'
           END AS line,
           sum(CAST(EffectiveCost AS DECIMAL(18, 6))) AS effective
    FROM focus GROUP BY line""").fetchall())
utilisation = dict(con.execute("""
    SELECT CommitmentDiscountId,
           100 * sum(CAST(CommitmentDiscountQuantity AS DECIMAL(18, 6))) FILTER (WHERE CommitmentDiscountStatus = 'Used')
               / sum(CAST(CommitmentDiscountQuantity AS DECIMAL(18, 6)))
    FROM focus WHERE ChargeCategory = 'Usage' AND CommitmentDiscountStatus IS NOT NULL
    GROUP BY CommitmentDiscountId""").fetchall())
coverage = dict(con.execute("""
    SELECT ServiceCategory,
           100 * coalesce(sum(CAST(ListCost AS DECIMAL(18, 6))) FILTER (WHERE CommitmentDiscountStatus = 'Used'), 0)
               / sum(CAST(ListCost AS DECIMAL(18, 6)))
    FROM focus WHERE ChargeCategory = 'Usage' AND coalesce(CommitmentDiscountStatus, '') <> 'Unused'
    GROUP BY ServiceCategory""").fetchall())
cents = lambda x: D(str(x)).quantize(D("0.01"))
mine = {l["name"]: D(l["effective"]) for l in billjoin["allocation"]["lines"]}
theirs = {k: cents(v) for k, v in showback.items() if cents(v) != 0}
assert mine == theirs, (mine, theirs)
for c in billjoin["commitments"]:
    assert cents(utilisation[c["id"]]) == D(c["utilisation"]), (c["id"], utilisation[c["id"]], c["utilisation"])
for c in billjoin["coverage"]:
    assert cents(coverage[c["service_category"]]) == D(c["coverage"]), (c, coverage)
print("duckdb %s: showback %s" % (duckdb.__version__, ", ".join("%s %s" % kv for kv in sorted(theirs.items()))))
print("utilisation %s; coverage %s" % (sorted((k.rsplit("/", 1)[-1].rsplit(":", 1)[-1], float(v)) for k, v in utilisation.items()), sorted((k, float(v)) for k, v in coverage.items())))
PY
sed 's/^/  /' "$WORK/duck.txt"

echo "▸ moto: start an AWS API emulator"
PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
"$VENV/bin/moto_server" -H 127.0.0.1 -p "$PORT" > "$WORK/moto.log" 2>&1 &
MOTO_PID=$!
for _ in $(seq 1 60); do
  curl -fsS -o /dev/null "http://127.0.0.1:$PORT/moto-api/" 2>/dev/null && break
  kill -0 "$MOTO_PID" 2>/dev/null || fail "moto_server exited: $(cat "$WORK/moto.log")"
  sleep 0.5
done
curl -fsS -o /dev/null "http://127.0.0.1:$PORT/moto-api/" || fail "moto_server did not answer on $PORT: $(cat "$WORK/moto.log")"
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_SESSION_TOKEN=test AWS_DEFAULT_REGION=us-east-1 AWS_PAGER="" AWS_EC2_METADATA_DISABLED=true
awsm() { "$AWS_BIN" --endpoint-url "http://127.0.0.1:$PORT" "$@"; }

echo "▸ AWS Organizations: store the team tag policy, attach it to the root, read it back and use the stored copy"
awsm organizations create-organization --feature-set ALL > "$WORK/org.json" || fail "create-organization: $(cat "$WORK/org.json")"
ROOT="$(awsm organizations list-roots --query 'Roots[0].Id' --output text)"
awsm organizations enable-policy-type --root-id "$ROOT" --policy-type TAG_POLICY > /dev/null || fail "enable-policy-type TAG_POLICY"
POLICY_ID="$(awsm organizations create-policy --name team-tag --type TAG_POLICY --description "allowed values of the team tag" --content "file://$F/tag-policy.json" --query 'Policy.PolicySummary.Id' --output text)"
awsm organizations attach-policy --policy-id "$POLICY_ID" --target-id "$ROOT" || fail "attach-policy"
awsm organizations list-policies-for-target --target-id "$ROOT" --filter TAG_POLICY --query 'Policies[].Name' --output text > "$WORK/attached.txt"
grep -qx "team-tag" "$WORK/attached.txt" || fail "team-tag should be attached to $ROOT: $(cat "$WORK/attached.txt")"
awsm organizations describe-policy --policy-id "$POLICY_ID" --query 'Policy.Content' --output text > "$WORK/stored-policy.json"
check "stored tag policy" "$F/tag-policy.json" "$WORK/stored-policy.json" <<'PY'
import json, sys
mine, stored = (json.load(open(p)) for p in sys.argv[1:3])
assert mine == stored, stored
print("policy %s attached to the root; allowed team values %s" % (sorted(stored["tags"]), stored["tags"]["team"]["tag_value"]["@@assign"]))
PY

echo "▸ EC2 and the Resource Groups Tagging API: never-tagged resources are invisible to get-resources, but not to the bill"
AMI="$(awsm ec2 describe-images --owners amazon --query 'Images[0].ImageId' --output text)"
[[ "$AMI" == ami-* ]] || fail "no AMI in moto: $AMI"
run_instance() {
  if [[ -n "$1" ]]; then
    awsm ec2 run-instances --image-id "$AMI" --instance-type t3.micro --tag-specifications "ResourceType=instance,Tags=[{Key=$1,Value=$2}]" --query 'Instances[0].InstanceId' --output text
  else
    awsm ec2 run-instances --image-id "$AMI" --instance-type t3.micro --query 'Instances[0].InstanceId' --output text
  fi
}
I_PAY="$(run_instance team payments)"
I_SEARCH="$(run_instance team search)"
I_CASE="$(run_instance team Search)"
I_NAME="$(run_instance Name legacy-batch)"
I_NEVER="$(run_instance "" "")"
awsm resourcegroupstaggingapi get-resources --resource-type-filters ec2:instance > "$WORK/resources.json"
awsm ec2 describe-instances > "$WORK/instances.json"
python3 - "$WORK/instances.json" "$WORK/bill.csv" <<'PY'
import csv, json, sys
columns = ["ChargeCategory", "ChargePeriodStart", "ChargePeriodEnd", "BilledCost", "EffectiveCost", "ListCost", "BillingCurrency", "ServiceName", "ServiceCategory", "ResourceId", "Tags"]
prices = ["2.40", "4.80", "9.60", "19.20", "38.40"]
instances = [i for r in json.load(open(sys.argv[1]))["Reservations"] for i in r["Instances"]]
with open(sys.argv[2], "w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for index, instance in enumerate(sorted(instances, key=lambda i: i["LaunchTime"])):
        tags = {t["Key"]: t["Value"] for t in instance.get("Tags") or []}
        cost = prices[index % len(prices)]
        writer.writerow({"ChargeCategory": "Usage", "ChargePeriodStart": "2026-10-01T00:00:00Z", "ChargePeriodEnd": "2026-10-02T00:00:00Z",
                         "BilledCost": cost, "EffectiveCost": cost, "ListCost": cost, "BillingCurrency": "USD", "ServiceName": "Amazon Elastic Compute Cloud",
                         "ServiceCategory": "Compute", "ResourceId": instance["InstanceId"], "Tags": json.dumps(tags) if tags else ""})
PY
CODE="$(exit_of "$WORK/tags.json" python3 code/main.py tags --resources "$WORK/resources.json" --tag-policy "$WORK/stored-policy.json" --bill "$WORK/bill.csv" --json)"
[[ "$CODE" == "1" ]] || fail "tags should exit 1 when resources need fixing, got $CODE: $(cat "$WORK/tags.json" "$WORK/tags.json.err")"
CODE="$(exit_of "$WORK/bill.json" python3 code/main.py report "$WORK/bill.csv" --tag-policy "$WORK/stored-policy.json" --json)"
[[ "$CODE" == "0" ]] || fail "report on the instance bill exit $CODE: $(cat "$WORK/bill.json.err")"
check "tag audit" "$WORK/tags.json" "$WORK/bill.json" "$I_PAY" "$I_SEARCH" "$I_CASE" "$I_NAME" "$I_NEVER" <<'PY'
import json, sys
audit, bill = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
pay, search, case, name, never = sys.argv[3:8]
listed = {a["resource"].rsplit("/", 1)[-1]: a for a in audit}
assert never not in listed, "get-resources listed a never-tagged instance: %s" % listed
assert {k for k, a in listed.items() if a["ok"]} == {pay, search}, listed
fix = {k for k, a in listed.items() if not a["ok"]}
assert fix == {case, name}, listed
assert listed[case]["status"] == "value 'Search' not allowed by the tag policy", listed[case]
untagged = {u["source"] for u in bill["allocation"]["untagged"]}
assert untagged == {case, name, never}, untagged
assert untagged - fix == {never}
order = [a["resource"].rsplit("/", 1)[-1] for a in audit if not a["ok"]]
assert [float(listed[k]["effective"]) for k in order] == sorted((float(listed[k]["effective"]) for k in order), reverse=True), audit
print("get-resources: %d instances, fix %s; the bill also charges never-tagged %s to (untagged)" % (len(listed), sorted(fix), never))
PY

echo "▸ Cost Explorer: get-cost-and-usage grouped by the team tag, compared with the FOCUS rows mapped from CUR"
python3 -c 'import json, sys; print(json.dumps({"results": [json.load(open(sys.argv[1]))]}))' "$F/ce-amortized-by-team.json" > "$WORK/ce-seed.json"
HTTP="$(curl -sS -o "$WORK/ce-seed.out" -w '%{http_code}' -X POST -H 'Content-Type: application/json' --data "@$WORK/ce-seed.json" "http://127.0.0.1:$PORT/moto-api/static/ce/cost-and-usage-results")"
[[ "$HTTP" == "201" ]] || fail "seeding moto's Cost Explorer results returned $HTTP: $(cat "$WORK/ce-seed.out")"
awsm ce get-cost-and-usage --time-period Start=2026-10-01,End=2026-10-15 --granularity MONTHLY --metrics AmortizedCost --group-by Type=TAG,Key=team > "$WORK/ce.json" || fail "ce get-cost-and-usage: $(cat "$WORK/ce.json")"
CODE="$(exit_of "$WORK/ce-diff.txt" python3 code/main.py ce-diff "$WORK/cur-focus.csv" --cost-explorer "$WORK/ce.json")"
[[ "$CODE" == "0" ]] || fail "ce-diff should agree with the amortised cost by tag (exit $CODE): $(cat "$WORK/ce-diff.txt" "$WORK/ce-diff.txt.err")"
sed 's/^/  /' "$WORK/ce-diff.txt"
grep -q 'team\$  ' "$WORK/ce-diff.txt" || fail "Cost Explorer's team\$ group (no tag) should be compared: $(cat "$WORK/ce-diff.txt")"
sed 's/"Amount": "252"/"Amount": "250"/' "$WORK/ce.json" > "$WORK/ce-tampered.json"
CODE="$(exit_of "$WORK/ce-tampered.txt" python3 code/main.py ce-diff "$WORK/cur-focus.csv" --cost-explorer "$WORK/ce-tampered.json")"
[[ "$CODE" == "1" ]] && grep -q "DIFF 2.00" "$WORK/ce-tampered.txt" || fail "a 2.00 difference on team\$search should exit 1 (got $CODE): $(cat "$WORK/ce-tampered.txt")"

echo "SMOKE OK: cloud/finops-focus"
