#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
F="code/fixtures"
SLOTH_VERSION="v0.16.0"
SLOTH_SHA256_AMD64=1cecaa50853abbb3669a928612b523df8174d258ea6f2a3c8f048e5fc5af90b2
SLOTH_SHA256_ARM64=7b570c465df3cc997e744dfcfcea83c3559bba6b12f4d6165994925419cbb41c
PROM_IMAGE="prom/prometheus:v3.15.1"
LABEL="cc-lesson=slo-burn-rate"
mkdir -p "$WORK/bin" "$WORK/dl" "$WORK/neg"
export PATH="$WORK/bin:$PATH"

cleanup() {
  set +e
  if command -v docker >/dev/null; then
    docker ps -aq --filter "label=$LABEL" > "$WORK/leftover.txt" 2>/dev/null
    if [[ -s "$WORK/leftover.txt" ]]; then xargs docker rm -f < "$WORK/leftover.txt" >/dev/null 2>&1; fi
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }
minislo() { python3 code/main.py "$@"; }
promtool() {
  local dir="$1"
  shift
  docker run --rm --label "$LABEL" --user "$(id -u):$(id -g)" -v "$dir:/work" -w /work --entrypoint /bin/promtool "$PROM_IMAGE" "$@"
}

compare_rules() {
  python3 - "$@" <<'PY'
import sys
sys.path.insert(0, "code")
import main

def summary(path):
    with open(path, encoding="utf-8") as handle:
        return main.inspect_rules(main.parse_yaml(handle.read())[0])

left, right = summary(sys.argv[1]), summary(sys.argv[2])
problems = []
if left["groups"] != right["groups"]:
    problems.append(f"groups differ: {left['groups']} vs {right['groups']}")
records = {(r["record"], r["window"]) for r in left["records"]} ^ {(r["record"], r["window"]) for r in right["records"]}
records -= {("sloth_slo_info", "")}
if records:
    problems.append(f"records in only one file: {sorted(records)}")
if left["optimized"] != right["optimized"]:
    problems.append(f"period SLI optimized {left['optimized']} vs {right['optimized']}")
if len(left["alerts"]) != len(right["alerts"]):
    problems.append(f"{len(left['alerts'])} alerts vs {len(right['alerts'])}")
for a, b in zip(left["alerts"], right["alerts"]):
    for key in ("alert", "severity", "for", "labels", "annotations"):
        if a[key] != b[key]:
            problems.append(f"alert {a['alert']}/{a['severity']} {key}: {a[key]} vs {b[key]}")
    if [(c["short"], c["long"]) for c in a["conditions"]] != [(c["short"], c["long"]) for c in b["conditions"]]:
        problems.append(f"alert {a['alert']}/{a['severity']} windows: {a['conditions']} vs {b['conditions']}")
    for c, d in zip(a["conditions"], b["conditions"]):
        if abs(c["threshold"] - d["threshold"]) > 1e-9 * abs(d["threshold"]):
            problems.append(f"alert {a['alert']}/{a['severity']} {c['long']}+{c['short']} threshold {c['threshold']!r} vs {d['threshold']!r}")
    print(f"  {a['alert']} [{a['severity']}]: " + "; ".join(f"{c['long']}+{c['short']} > {c['threshold']:.6g} vs {d['threshold']:.6g}" for c, d in zip(a["conditions"], b["conditions"])))
print(f"  {len(left['records'])} vs {len(right['records'])} recording rules; budget {left['budget']!r} vs {right['budget']!r}")
if problems:
    print("\n".join(problems))
    sys.exit(1)
PY
}

echo "▸ minislo table: the SRE workbook's burn factors for 99.9% over 30d"
minislo table "$F/slo.yaml" --json > "$WORK/table.json" || fail "table: $(cat "$WORK/table.json")"
python3 - "$WORK/table.json" <<'PY' || fail "burn table: $(cat "$WORK/table.json")"
import json, sys
rows = json.load(open(sys.argv[1]))["checkout-requests-availability"]
assert [r["burn_factor"] for r in rows] == [14.4, 6, 3, 1], [r["burn_factor"] for r in rows]
assert abs(rows[0]["detection_seconds_at_100_percent_errors"] - 51.84) < 1e-9, rows[0]
assert all(abs(r["budget_spent_when_it_fires"] - r["budget_percent"] / 100) < 1e-12 for r in rows), rows
PY
minislo table "$F/slo.yaml"

echo "▸ minislo budget: ship at 21% spent (exit 0), freeze when the budget is gone (exit 1)"
minislo budget "$F/slo.yaml" --total 42000000 --bad 30000 --elapsed 9d > "$WORK/budget-ok.txt" || fail "budget should exit 0: $(cat "$WORK/budget-ok.txt")"
grep -q 'policy           ship' "$WORK/budget-ok.txt" || fail "budget decision: $(cat "$WORK/budget-ok.txt")"
CODE=0
minislo budget "$F/slo.yaml" --total 140000000 --bad 151000 > "$WORK/budget-freeze.txt" || CODE=$?
[[ "$CODE" == "1" ]] && grep -q 'policy           freeze' "$WORK/budget-freeze.txt" || fail "an exhausted budget should freeze with exit 1, got $CODE: $(cat "$WORK/budget-freeze.txt")"

echo "▸ minislo simulate: when each alert fires for a fast burn, a slow burn and no burn"
minislo simulate "$F/slo.yaml" --scenarios "$F/scenarios.yaml" > "$WORK/simulate.txt" || fail "simulate: $(cat "$WORK/simulate.txt")"
sed 's/^/  /' "$WORK/simulate.txt"
grep -Eq '^ +3d43m +page fires \(quick\)' "$WORK/simulate.txt" || fail "the fast burn should page at 3d43m: $(cat "$WORK/simulate.txt")"
grep -Eq '^ +3d8h +ticket fires \(slow\)' "$WORK/simulate.txt" || fail "the slow burn should open a ticket at 3d8h: $(cat "$WORK/simulate.txt")"

echo "▸ minislo generate: recording and alerting rules in Sloth's naming"
minislo generate "$F/slo.yaml" -o "$WORK/minislo-rules.yaml" 2> "$WORK/generate.err" || fail "generate: $(cat "$WORK/generate.err")"
minislo inspect "$WORK/minislo-rules.yaml" | sed 's/^/  /'

case "$(uname -m)" in
  x86_64 | amd64) ARCH=amd64 PINNED="$SLOTH_SHA256_AMD64" ;;
  aarch64 | arm64) ARCH=arm64 PINNED="$SLOTH_SHA256_ARM64" ;;
  *) fail "no pinned Sloth build for $(uname -m)" ;;
esac
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
[[ "$OS" == "linux" ]] || fail "the smoke test downloads the linux build of Sloth; run it on Linux (got $OS)"
ASSET="sloth-linux-$ARCH"
BASE="https://github.com/slok/sloth/releases/download/$SLOTH_VERSION"
echo "▸ download Sloth $SLOTH_VERSION ($ASSET) and verify it against checksums.txt and the pinned sha256"
curl -fsSL --retry 3 -o "$WORK/dl/$ASSET" "$BASE/$ASSET" || fail "download $BASE/$ASSET"
curl -fsSL --retry 3 -o "$WORK/dl/checksums.txt" "$BASE/checksums.txt" || fail "download $BASE/checksums.txt"
WANT="$(awk -v f="$ASSET" '$2 == f {print $1}' "$WORK/dl/checksums.txt")"
GOT="$(sha256_of "$WORK/dl/$ASSET")"
[[ -n "$WANT" && "$GOT" == "$WANT" && "$GOT" == "$PINNED" ]] || fail "sha256 of $ASSET is $GOT; checksums.txt says ${WANT:-nothing}; pinned $PINNED"
install -m 0755 "$WORK/dl/$ASSET" "$WORK/bin/sloth"
sloth version > "$WORK/sloth-version.txt" 2>&1 || fail "sloth version: $(cat "$WORK/sloth-version.txt")"
grep -q "${SLOTH_VERSION#v}" "$WORK/sloth-version.txt" || fail "sloth version printed: $(cat "$WORK/sloth-version.txt")"
echo "  sloth $(cat "$WORK/sloth-version.txt")"

echo "▸ sloth validate and sloth generate on the same spec"
sloth validate -i "$F/slo.yaml" > "$WORK/sloth-validate.txt" 2>&1 || fail "sloth validate: $(cat "$WORK/sloth-validate.txt")"
sloth generate -i "$F/slo.yaml" -o "$WORK/sloth-rules.yaml" > "$WORK/sloth-generate.txt" 2>&1 || fail "sloth generate: $(cat "$WORK/sloth-generate.txt")"
[[ -s "$WORK/sloth-rules.yaml" ]] || fail "sloth wrote no rules: $(cat "$WORK/sloth-generate.txt")"
minislo inspect "$WORK/sloth-rules.yaml" | sed 's/^/  /'

echo "▸ minislo's rules and Sloth's: same groups, records, windows, alert labels, annotations and thresholds"
compare_rules "$WORK/minislo-rules.yaml" "$WORK/sloth-rules.yaml" > "$WORK/compare.txt" 2>&1 || fail "minislo and Sloth rules differ: $(cat "$WORK/compare.txt")"
cat "$WORK/compare.txt"
echo "▸ the committed fixture code/fixtures/sloth-rules.yaml matches what Sloth $SLOTH_VERSION writes"
compare_rules "$F/sloth-rules.yaml" "$WORK/sloth-rules.yaml" > "$WORK/compare-fixture.txt" 2>&1 || fail "fixture and Sloth's output differ: $(cat "$WORK/compare-fixture.txt")"

command -v docker >/dev/null || fail "docker is required for promtool"
echo "▸ pull $PROM_IMAGE for promtool"
docker pull -q "$PROM_IMAGE" > "$WORK/pull.txt" 2>&1 || docker pull -q "$PROM_IMAGE" > "$WORK/pull.txt" 2>&1 || fail "docker pull $PROM_IMAGE: $(cat "$WORK/pull.txt")"
cp "$F/sloth-rules.yaml" "$WORK/fixture-rules.yaml"

echo "▸ promtool check rules: both generated files and the fixture load"
for pair in minislo-rules.yaml:16 sloth-rules.yaml:17 fixture-rules.yaml:17; do
  file="${pair%%:*}" want="${pair##*:}"
  promtool "$WORK" check rules "$file" > "$WORK/check-$file.txt" 2>&1 || fail "promtool check rules $file: $(cat "$WORK/check-$file.txt")"
  grep -q "SUCCESS: $want rules found" "$WORK/check-$file.txt" || fail "expected $want rules in $file: $(cat "$WORK/check-$file.txt")"
  echo "  $file: $want rules"
done

echo "▸ minislo test-file: one promtool test per rule set, expectations from the simulator"
for rules in minislo sloth; do
  minislo test-file "$F/slo.yaml" --scenarios "$F/scenarios.yaml" --rules "$WORK/$rules-rules.yaml" -o "$WORK/$rules-test.yaml" 2> "$WORK/$rules-test.err" || fail "test-file for $rules: $(cat "$WORK/$rules-test.err")"
  [[ ! -s "$WORK/$rules-test.err" ]] || echo "  $(cat "$WORK/$rules-test.err")"
done
python3 - "$WORK/minislo-test.yaml" "$WORK/sloth-test.yaml" <<'PY' || fail "the two test files should expect the same alerts and values"
import sys
sys.path.insert(0, "code")
import main
docs = [main.parse_yaml(open(path, encoding="utf-8").read())[0] for path in sys.argv[1:]]
for doc in docs:
    doc.pop("rule_files")
assert docs[0] == docs[1], "expectations differ between the two test files"
tests = docs[0]["tests"]
alerts = sum(len(t["alert_rule_test"]) for t in tests)
firing = sum(1 for t in tests for a in t["alert_rule_test"] if a["exp_alerts"])
values = sum(len(t["promql_expr_test"]) for t in tests)
print(f"  {len(tests)} scenarios, {alerts} alert checkpoints ({firing} with an alert firing), {values} recording-rule value checks")
PY

echo "▸ promtool test rules: minislo's and Sloth's rules fire the same alerts at the same minutes and record the same values"
for rules in minislo sloth; do
  promtool "$WORK" test rules "$rules-test.yaml" > "$WORK/promtool-$rules.txt" 2>&1 || fail "promtool test rules for $rules: $(head -c 4000 "$WORK/promtool-$rules.txt")"
  grep -q "SUCCESS" "$WORK/promtool-$rules.txt" || fail "promtool did not report SUCCESS for $rules: $(cat "$WORK/promtool-$rules.txt")"
  echo "  $rules: SUCCESS"
done

echo "▸ negative control: a page threshold of 28.8x instead of 14.4x must fail the same test at 3d43m"
sed 's/(14.4 \*/(28.8 */' "$WORK/sloth-rules.yaml" > "$WORK/neg/sloth-rules.yaml"
grep -q '(28.8 \*' "$WORK/neg/sloth-rules.yaml" || fail "could not change the page threshold in Sloth's rules"
cp "$WORK/sloth-test.yaml" "$WORK/neg/sloth-test.yaml"
CODE=0
promtool "$WORK/neg" test rules sloth-test.yaml > "$WORK/neg.txt" 2>&1 || CODE=$?
[[ "$CODE" != "0" ]] || fail "promtool accepted a page threshold twice as high: $(head -c 2000 "$WORK/neg.txt")"
grep -q "time: 3d43m" "$WORK/neg.txt" && grep -q "alertname: CheckoutAvailabilityBudgetBurn" "$WORK/neg.txt" || fail "the failure should name the alert at 3d43m: $(head -c 3000 "$WORK/neg.txt")"
echo "  promtool exit $CODE, first failure: $(grep -m1 'time:' "$WORK/neg.txt" | sed 's/^ *//')"

echo "▸ negative control: an error budget of 0.002 instead of 0.001 must fail the burn-rate value checks"
sed 's/vector(0.001)/vector(0.002)/' "$WORK/minislo-rules.yaml" > "$WORK/neg/minislo-rules.yaml"
cp "$WORK/minislo-test.yaml" "$WORK/neg/minislo-test.yaml"
CODE=0
promtool "$WORK/neg" test rules minislo-test.yaml > "$WORK/neg-values.txt" 2>&1 || CODE=$?
[[ "$CODE" != "0" ]] && grep -q "slo:current_burn_rate:ratio" "$WORK/neg-values.txt" || fail "promtool should reject the wrong burn-rate values (exit $CODE): $(head -c 3000 "$WORK/neg-values.txt")"
echo "  promtool exit $CODE on the current burn rate"

echo "SMOKE OK: observability/slo-burn-rate"
