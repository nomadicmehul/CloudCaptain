#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
LESSON="$PWD"
MD="$LESSON/code/main.py"
EVIDENTLY_VERSION="0.7.23"
SCIPY_VERSION="1.18.1"
PROM_IMAGE="prom/prometheus:v3.16.0@sha256:e04987641b57704f408e31f6373ba4138ecf27cb4a165d24aabb6c38fddd0f06"
PROMTOOL_NAME="cc-drift-monitoring-promtool"
WORK="$(mktemp -d)"
DATA="$WORK/data"
SERVER_PID=""
cleanup() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if command -v docker >/dev/null; then
    docker rm -f "$PROMTOOL_NAME" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

md() { python3 "$MD" "$@"; }
promtool() { docker run --rm -i --name "$PROMTOOL_NAME" --entrypoint /bin/promtool "$PROM_IMAGE" "$@"; }
promtool_dir() {
  tar -C "$1" -cf - . | docker run --rm -i --name "$PROMTOOL_NAME" --entrypoint /bin/sh "$PROM_IMAGE" \
    -c 'mkdir -p /tmp/w && cd /tmp/w && tar -xf - && /bin/promtool test rules tests.yml'
}

echo "▸ $(python3 --version), $(docker --version)"
python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' \
  || fail "the oracle wheels are pinned for CPython 3.12, found $(python3 --version)"

echo "▸ minidrift generate: 2,000 reference and 2,000 current rows (seed 7) with planted shifts"
md generate --out "$DATA" --rows 2000 --seed 7 > "$WORK/generate.json" || fail "generate: $(cat "$WORK/generate.json")"
ARGS=("$DATA/reference.csv" "$DATA/current.csv" --schema "$DATA/schema.json" --labels "$DATA/labels.csv" --model churn)

echo "▸ minidrift report flags exactly the planted columns with the default methods"
md report "${ARGS[@]}" > "$WORK/report.txt" || fail "report: $(cat "$WORK/report.txt")"
sed 's/^/  /' "$WORK/report.txt"
md report "${ARGS[@]}" --json > "$WORK/mini.json" || fail "report --json"
md report "${ARGS[@]}" --json --num-method ks --cat-method chisquare > "$WORK/mini-ks.json" || fail "report --json ks/chisquare"
python3 - "$WORK/mini.json" "$WORK/mini-ks.json" <<'PY' || fail "minidrift did not flag the planted shifts: $(cat "$WORK/report.txt")"
import json, sys
default, pvalue = (json.load(open(p)) for p in sys.argv[1:3])
drifted = lambda r: sorted(c["column"] for c in r["columns"] if c["drifted"])
assert drifted(default) == ["income", "message", "plan", "prediction"], drifted(default)
assert drifted(pvalue) == ["income", "message", "plan", "prediction", "region"], drifted(pvalue)
issues = {q["column"]: " ".join(q["issues"]) for q in default["quality"]}
assert "missing rate rose" in issues["age"], issues
assert "not numerical" in issues["tenure_days"], issues
assert "new category: apac" in issues["region"], issues
perf = default["performance"]
assert perf["status"] == "decayed" and 0.3 < perf["coverage"] < 0.5, perf
assert default["dataset"]["drift"] and default["dataset"]["share"] == 0.5, default["dataset"]
PY
set +e
md report "${ARGS[@]}" --fail-on-drift > /dev/null
CODE=$?
set -e
[[ "$CODE" == "1" ]] || fail "report --fail-on-drift should exit 1 on drift, got $CODE"

echo "▸ install scipy $SCIPY_VERSION and Evidently $EVIDENTLY_VERSION with every dependency pinned by sha256"
python3 -m venv "$WORK/oracles" || fail "python3 -m venv"
"$WORK/oracles/bin/pip" install --quiet --disable-pip-version-check --retries 3 --require-hashes --only-binary=:all: \
  -r "$LESSON/code/requirements-oracles.txt" > "$WORK/pip.txt" 2>&1 || fail "pip install oracles: $(tail -20 "$WORK/pip.txt")"
"$WORK/oracles/bin/python" - "$EVIDENTLY_VERSION" "$SCIPY_VERSION" <<'PY' || fail "unexpected oracle versions"
import sys
from importlib.metadata import version
found = (version("evidently"), version("scipy"))
print(f"  evidently {found[0]}, scipy {found[1]}, pandas {version('pandas')}, numpy {version('numpy')}")
assert found == tuple(sys.argv[1:3]), found
PY

echo "▸ the same statistics from minidrift, Evidently ValueDrift/DriftedColumnsCount and scipy.stats agree"
"$WORK/oracles/bin/python" - "$DATA/reference.csv" "$DATA/current.csv" "$WORK/mini.json" "$WORK/mini-ks.json" > "$WORK/oracle.txt" 2>&1 <<'PY' || fail "oracles disagree: $(cat "$WORK/oracle.txt")"
import json, math, re, sys, warnings
warnings.filterwarnings("ignore")
import pandas as pd
from scipy import stats
from scipy.spatial import distance
from evidently import Dataset, DataDefinition, Report
from evidently.metrics import DriftedColumnsCount, ValueDrift

ref_path, cur_path, mini_path, mini_ks_path = sys.argv[1:5]
numerical, categorical = ["age", "income", "tenure_days", "prediction"], ["plan", "region"]
features = ["age", "income", "tenure_days", "plan", "region"]

def load(path):
    df = pd.read_csv(path, dtype={c: str for c in categorical + ["message"]}, keep_default_na=False, na_values=[""])
    for c in numerical:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

ref, cur = load(ref_path), load(cur_path)
mini = {c["column"]: c for c in json.load(open(mini_path))["columns"]}
mini_ks = {c["column"]: c for c in json.load(open(mini_ks_path))["columns"]}
num_keys = {"psi": "psi", "ks": "ks_p_value", "wasserstein": "wasserstein_norm", "jensenshannon": "jensenshannon"}
cat_keys = {"psi": "psi", "chisquare": "chisquare_p_value", "jensenshannon": "jensenshannon"}
plan = [(c, m, k) for c in numerical for m, k in num_keys.items()] + [(c, m, k) for c in categorical for m, k in cat_keys.items()]
thresholds = {"psi": 0.1, "ks": 0.05, "wasserstein": 0.1, "jensenshannon": 0.1, "chisquare": 0.05}
dd = DataDefinition(numerical_columns=numerical, categorical_columns=categorical)
metrics = [ValueDrift(column=c, method=m) for c, m, _ in plan]
metrics += [DriftedColumnsCount(columns=features, num_method="ks", cat_method="chisquare"), DriftedColumnsCount(columns=features)]
snapshot = Report(metrics).run(Dataset.from_pandas(cur[numerical + categorical], data_definition=dd),
                               Dataset.from_pandas(ref[numerical + categorical], data_definition=dd)).dict()
values = [m["value"] for m in snapshot["metrics"]]
errors = []

def close(a, b, rel, floor=1e-15):
    return (math.isinf(a) and math.isinf(b)) or abs(a - b) <= max(rel * max(abs(a), abs(b)), floor)

def flagged(method, value):
    return value < thresholds[method] if method in ("ks", "chisquare") else value >= thresholds[method]

for (column, method, key), theirs in zip(plan, values):
    mine = mini[column]["stats"][key]
    if not close(mine, float(theirs), 1e-6 if method == "chisquare" else 1e-9) or flagged(method, mine) != flagged(method, theirs):
        errors.append(f"{column} {method}: minidrift {mine!r} vs Evidently {theirs!r}")
    print(f"  {column:<12} {method:<14} minidrift {mine:<22.10g} Evidently {float(theirs):<22.10g} {'DRIFT' if flagged(method, theirs) else '-'}")
for (label, source), counted in zip((("ks/chisquare", mini_ks), ("default", mini)), values[-2:]):
    mine = sorted(c for c in features if source[c]["drifted"])
    print(f"  DriftedColumnsCount {label}: Evidently {int(counted['count'])} ({counted['share']:.0%}), minidrift {len(mine)} {mine}")
    if int(counted["count"]) != len(mine) or not close(counted["share"], len(mine) / len(features), 1e-12):
        errors.append(f"DriftedColumnsCount {label}: Evidently {counted} vs minidrift {mine}")
for column in numerical:
    r, c = ref[column].dropna().to_numpy(), cur[column].dropna().to_numpy()
    ks, w, s = stats.ks_2samp(r, c), stats.wasserstein_distance(r, c), mini[column]["stats"]
    for name, mine, theirs, rel in (("ks statistic", s["ks_statistic"], ks.statistic, 1e-12), ("ks p-value", s["ks_p_value"], ks.pvalue, 1e-9),
                                    ("wasserstein", s["wasserstein"], w, 1e-9)):
        if not close(mine, float(theirs), rel):
            errors.append(f"scipy {column} {name}: minidrift {mine!r} vs scipy {float(theirs)!r}")
    print(f"  scipy {column:<12} n={len(r)}/{len(c)} ks D={float(ks.statistic):.5f} p={float(ks.pvalue):.4g} wasserstein={float(w):.6g}")
for column in categorical:
    r, c, s = ref[column], cur[column], mini[column]["stats"]
    keys = sorted(set(r) | set(c))
    chi = stats.chisquare([int((c == k).sum()) for k in keys], [int((r == k).sum()) * len(c) / len(r) for k in keys])
    js = distance.jensenshannon([float((r == k).mean()) for k in keys], [float((c == k).mean()) for k in keys])
    if not (close(s["chisquare_statistic"], float(chi.statistic), 1e-9) and close(s["chisquare_p_value"], float(chi.pvalue), 1e-6, 1e-300)):
        errors.append(f"scipy {column} chisquare: minidrift {s['chisquare_statistic']!r} p={s['chisquare_p_value']!r} vs {chi}")
    if not close(s["jensenshannon"], float(js), 1e-9):
        errors.append(f"scipy {column} jensenshannon: minidrift {s['jensenshannon']!r} vs scipy {float(js)!r}")
    print(f"  scipy {column:<12} chisquare={float(chi.statistic):.6g} p={float(chi.pvalue):.4g} js={float(js):.6g}")
tokens = lambda text: re.findall(r"[a-z0-9']+", text.lower())
ref_tokens, cur_tokens = [tokens(t) for t in ref["message"]], [tokens(t) for t in cur["message"]]
length = stats.ks_2samp([len(t) for t in ref_tokens], [len(t) for t in cur_tokens])
vocabulary = sorted({t for row in ref_tokens + cur_tokens for t in row})
ref_counts = pd.Series([t for row in ref_tokens for t in row]).value_counts()
cur_counts = pd.Series([t for row in cur_tokens for t in row]).value_counts()
token_js = distance.jensenshannon([int(ref_counts.get(t, 0)) for t in vocabulary], [int(cur_counts.get(t, 0)) for t in vocabulary])
s = mini["message"]["stats"]
for name, mine, theirs in (("length ks p-value", s["length_ks_p_value"], length.pvalue), ("token js", s["token_jensenshannon"], token_js)):
    if not close(mine, float(theirs), 1e-9):
        errors.append(f"scipy message {name}: minidrift {mine!r} vs scipy {float(theirs)!r}")
print(f"  scipy message      length ks p={float(length.pvalue):.4g} token js={float(token_js):.6g}")
if errors:
    print("MISMATCH:\n  " + "\n  ".join(errors))
    sys.exit(1)
print(f"  {len(plan)} Evidently values, 2 drifted-column counts and {len(numerical) * 3 + len(categorical) * 2 + 2} scipy values agree")
PY
cat "$WORK/oracle.txt"

echo "▸ minidrift serve: /metrics on a port the kernel picked"
python3 "$MD" serve "${ARGS[@]}" --port 0 --port-file "$WORK/port" > "$WORK/serve.log" 2>&1 &
SERVER_PID=$!
deadline=$((SECONDS + 30))
until [[ -s "$WORK/port" ]] && curl -sf -o "$WORK/health.txt" "http://127.0.0.1:$(cat "$WORK/port")/healthz"; do
  kill -0 "$SERVER_PID" 2>/dev/null || fail "minidrift serve exited: $(cat "$WORK/serve.log")"
  (( SECONDS < deadline )) || fail "minidrift serve did not answer within 30 s: $(cat "$WORK/serve.log")"
  sleep 0.2
done
[[ "$(cat "$WORK/health.txt")" == "ok" ]] || fail "/healthz answered: $(cat "$WORK/health.txt")"
URL="http://127.0.0.1:$(cat "$WORK/port")"
curl -sf -D "$WORK/headers.txt" -o "$WORK/metrics.txt" "$URL/metrics" || fail "GET /metrics"
grep -qi '^content-type: text/plain; version=0.0.4' "$WORK/headers.txt" || fail "content type: $(cat "$WORK/headers.txt")"
grep -q '^minidrift_feature_drift_detected{model="churn",method="wasserstein",feature="income",kind="numerical",role="feature"} 1.0$' "$WORK/metrics.txt" \
  || fail "income drift gauge missing: $(grep drift_detected "$WORK/metrics.txt")"
echo "  $(grep -c '^minidrift_' "$WORK/metrics.txt") samples in $(grep -c '^# TYPE' "$WORK/metrics.txt") gauges"

echo "▸ promtool check metrics (Prometheus ${PROM_IMAGE%%@*}) lints the exposition"
docker pull -q "$PROM_IMAGE" > "$WORK/pull.txt" 2>&1 || docker pull -q "$PROM_IMAGE" > "$WORK/pull.txt" 2>&1 || fail "docker pull: $(cat "$WORK/pull.txt")"
promtool check metrics < "$WORK/metrics.txt" > "$WORK/lint.txt" 2>&1 || fail "promtool check metrics: $(cat "$WORK/lint.txt")"
[[ ! -s "$WORK/lint.txt" ]] || fail "promtool check metrics reported problems: $(cat "$WORK/lint.txt")"
{ cat "$WORK/metrics.txt"; printf '# HELP minidrift_scrapes Scrapes.\n# TYPE minidrift_scrapes counter\nminidrift_scrapes 3\n'; } > "$WORK/bad-metrics.txt"
set +e
promtool check metrics < "$WORK/bad-metrics.txt" > "$WORK/lint-bad.txt" 2>&1
CODE=$?
set -e
[[ "$CODE" != "0" ]] && grep -q '_total' "$WORK/lint-bad.txt" || fail "promtool should reject a counter without _total (exit $CODE): $(cat "$WORK/lint-bad.txt")"
echo "  clean; a planted counter without _total is rejected: $(head -1 "$WORK/lint-bad.txt")"

echo "▸ minidrift rules pass promtool check rules"
mkdir -p "$WORK/prom"
md rules > "$WORK/prom/rules.yml"
promtool check rules /dev/stdin < "$WORK/prom/rules.yml" > "$WORK/check-rules.txt" 2>&1 || fail "promtool check rules: $(cat "$WORK/check-rules.txt")"
grep -q 'SUCCESS: 5 rules found' "$WORK/check-rules.txt" || fail "promtool check rules: $(cat "$WORK/check-rules.txt")"

echo "▸ promtool test rules: the scraped gauges fire the drift alerts after 30m, not before"
python3 - "$WORK/metrics.txt" "$WORK/mini.json" "$WORK/prom" <<'PY' > "$WORK/expected.txt" || fail "building the rule test failed"
import json, sys
metrics_path, report_path, out = sys.argv[1:4]
keep = {"minidrift_feature_drift_detected", "minidrift_dataset_drift_share", "minidrift_feature_missing_ratio", "minidrift_last_run_timestamp_seconds"}
series = []
for line in open(metrics_path):
    if line.startswith("#") or not line.strip():
        continue
    selector, value = line.rsplit(" ", 1)
    if selector.split("{", 1)[0] in keep:
        series.append({"series": selector, "values": f"{float(value)!r}x60"})
report = json.load(open(report_path))
model = report["model"]
def column_alert(c, summary):
    labels = {"severity": "ticket", "model": model, "feature": c["column"], "method": c["method"], "kind": c["kind"], "role": c["role"]}
    return {"exp_labels": labels, "exp_annotations": {"summary": summary}}
ds = report["dataset"]
expected = {
    "FeatureDrift": [column_alert(c, f"{c['column']} of {model} drifted ({c['method']})") for c in report["columns"] if c["role"] == "feature" and c["drifted"]],
    "PredictionDrift": [column_alert(c, f"predictions of {model} drifted ({c['method']})") for c in report["columns"] if c["role"] == "prediction" and c["drifted"]],
    "DatasetDrift": [{"exp_labels": {"severity": "page", "model": model}, "exp_annotations": {"summary": f"{ds['share'] * 100:.4g}% of the features of {model} drifted"}}]
    if ds["share"] >= ds["drift_share"] else [],
    "FeatureMissingValues": [{"exp_labels": {"severity": "ticket", "model": model, "feature": q["column"]},
                              "exp_annotations": {"summary": f"{q['column']} of {model} lost values: a pipeline bug until proven otherwise"}}
                             for q in report["quality"] if q["missing_current"] - q["missing_reference"] > 0.05],
    "DriftCheckStale": [],
}
cases = [{"eval_time": "10m", "alertname": name, "exp_alerts": []} for name in expected]
cases += [{"eval_time": "45m", "alertname": name, "exp_alerts": alerts} for name, alerts in expected.items()]
test = {"rule_files": ["rules.yml"], "evaluation_interval": "1m", "tests": [{"interval": "1m", "input_series": series, "alert_rule_test": cases}]}
json.dump(test, open(f"{out}/tests.yml", "w"), indent=1)
cases[-5]["exp_alerts"] = cases[-5]["exp_alerts"][1:]
json.dump(test, open(f"{out}/tests-wrong.yml", "w"), indent=1)
print(" ".join(f"{name}={len(alerts)}" for name, alerts in expected.items()))
PY
grep -q 'FeatureDrift=3 PredictionDrift=1 DatasetDrift=1 FeatureMissingValues=1 DriftCheckStale=0' "$WORK/expected.txt" || fail "expected alerts: $(cat "$WORK/expected.txt")"
promtool_dir "$WORK/prom" > "$WORK/test-rules.txt" 2>&1 || fail "promtool test rules: $(cat "$WORK/test-rules.txt")"
grep -q SUCCESS "$WORK/test-rules.txt" || fail "promtool test rules: $(cat "$WORK/test-rules.txt")"
echo "  fired at 45m: $(cat "$WORK/expected.txt")"
mv "$WORK/prom/tests-wrong.yml" "$WORK/prom/tests.yml"
set +e
promtool_dir "$WORK/prom" > "$WORK/test-wrong.txt" 2>&1
CODE=$?
set -e
[[ "$CODE" != "0" ]] || fail "promtool test rules passed with one FeatureDrift alert missing from the expectation"
echo "  an expectation missing one FeatureDrift alert fails, as it should (exit $CODE)"

echo "▸ minidrift serve recomputes when the current window changes: no drift against itself"
cp "$DATA/reference.csv" "$DATA/current.csv"
touch -d '+1 minute' "$DATA/current.csv" 2>/dev/null || touch "$DATA/current.csv"
curl -sf -o "$WORK/metrics2.txt" "$URL/metrics" || fail "GET /metrics after the change"
grep -q '^minidrift_dataset_drift_share{model="churn"} 0.0$' "$WORK/metrics2.txt" || fail "drift share after reload: $(grep -E 'drift_share|drift_detected' "$WORK/metrics2.txt")"
grep -q 'feature="income",kind="numerical",role="feature"} 0.0$' "$WORK/metrics2.txt" || fail "income still drifted after reload"

echo "SMOKE OK: mlops/drift-monitoring"
