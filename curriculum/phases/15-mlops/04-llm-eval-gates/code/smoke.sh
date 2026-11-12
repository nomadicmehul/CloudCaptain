#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
STUB_PID=""
SUITE="code/suites/ops-assistant.json"
PROMPTFOO_VERSION="0.125.1"
PROMPTFOO_SHA512="sha512-nogBhixdp69icwkg1EI0syZ4zAmAjJyWeLcqxAkTldbHi0qirOslwHvkUQhQmg65lQLrIKyMOMLXlBlSc6LRrg=="
MODELS=(ops-v1 ops-v2-regressed ops-v2-improved ops-v2-large)
cleanup() {
  if [[ -n "$STUB_PID" ]]; then
    kill "$STUB_PID" 2>/dev/null || true
    wait "$STUB_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

me() { python3 code/main.py "$@"; }
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

echo "▸ contamination: two suite cases sit verbatim in the training sample, a paraphrase does not count"
CODE="$(exit_of "$WORK/contam.json" me contamination --suite "$SUITE" --train code/fixtures/train-sample.jsonl --n 8 --threshold 0.5 --json)"
[[ "$CODE" == "1" ]] || fail "contamination should exit 1 when a case is flagged, got $CODE: $(cat "$WORK/contam.json" "$WORK/contam.json.err")"
check "contamination" "$WORK/contam.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
rows = {r["id"]: r for r in doc["cases"]}
assert doc["flagged"] == ["slo-downtime", "redact-email"], doc["flagged"]
assert rows["k8s-apiserver-port"]["matched"] == 0, rows["k8s-apiserver-port"]
assert 0 < rows["incident-json"]["overlap"] < 0.5, rows["incident-json"]
print("flagged %s; incident-json shares %.0f%% of its 8-grams and stays under the threshold" % (", ".join(doc["flagged"]), 100 * rows["incident-json"]["overlap"]))
PY

echo "▸ start the stub model server: four scripted model versions and a judge behind an OpenAI-compatible API"
me serve-stub --suite "$SUITE" --answers code/fixtures/stub-answers.json --port 0 --port-file "$WORK/port" > "$WORK/stub.log" 2>&1 &
STUB_PID=$!
DEADLINE=$((SECONDS + 30))
until [[ -s "$WORK/port" ]]; do
  kill -0 "$STUB_PID" 2>/dev/null || fail "stub server exited: $(cat "$WORK/stub.log")"
  (( SECONDS < DEADLINE )) || fail "stub server did not write its port within 30 s: $(cat "$WORK/stub.log")"
  sleep 0.2
done
PORT="$(cat "$WORK/port")"
BASE="http://127.0.0.1:$PORT/v1"
check "stub /v1/models" "$BASE" <<'PY'
import json, sys, urllib.request
doc = json.load(urllib.request.urlopen(sys.argv[1] + "/models", timeout=10))
ids = sorted(m["id"] for m in doc["data"])
assert ids == ["judge-v1", "ops-v1", "ops-v2-improved", "ops-v2-large", "ops-v2-regressed"], ids
print("%s/models lists %s" % (sys.argv[1], ", ".join(ids)))
PY

echo "▸ minieval run: the 20-case suite against each model, rubric cases graded by judge-v1 over HTTP, JSON and JUnit reports"
for model in "${MODELS[@]}"; do
  CODE="$(exit_of "$WORK/run-$model.txt" me run --suite "$SUITE" --base-url "$BASE" --model "$model" --judge judge-v1 --out "$WORK/$model.json" --junit "$WORK/$model.xml")"
  [[ "$CODE" == "0" ]] || fail "minieval run $model exit $CODE: $(cat "$WORK/run-$model.txt" "$WORK/run-$model.txt.err")"
  echo "  $model: $(grep '^pass rate' "$WORK/run-$model.txt")"
done
check "run results" "$WORK" <<'PY'
import json, os, sys
import xml.etree.ElementTree as ET
work = sys.argv[1]
expected = {"ops-v1": 13, "ops-v2-regressed": 6, "ops-v2-improved": 19, "ops-v2-large": 19}
base_fail = {"gib-to-mib", "slo-downtime", "cron-every-15", "pod-status-json", "disk-full-runbook", "p95-definition", "backup-rpo"}
for model, passed in expected.items():
    doc = json.load(open(os.path.join(work, model + ".json")))
    s = doc["summary"]
    assert (s["passed"], s["cases"], s["errors"]) == (passed, 20, 0), (model, s)
    root = ET.parse(os.path.join(work, model + ".xml")).getroot()
    assert (root.get("tests"), root.get("failures")) == ("20", str(20 - passed)), (model, root.attrib)
    failed = {c["id"] for c in doc["cases"] if not c["passed"]}
    if model == "ops-v1":
        assert failed == base_fail, failed
print("pass counts %s; JUnit failure counts agree" % ", ".join("%s %d/20" % kv for kv in expected.items()))
PY

echo "▸ minieval gate: the regressed candidate is rejected, the improved one accepted, the expensive one rejected on cost"
CODE="$(exit_of "$WORK/gate-regressed.json" me gate --baseline "$WORK/ops-v1.json" --candidate "$WORK/ops-v2-regressed.json" --json --junit "$WORK/gate-regressed.xml")"
[[ "$CODE" == "1" ]] || fail "gate on ops-v2-regressed should exit 1, got $CODE: $(cat "$WORK/gate-regressed.json")"
CODE="$(exit_of "$WORK/gate-improved.json" me gate --baseline "$WORK/ops-v1.json" --candidate "$WORK/ops-v2-improved.json" --json)"
[[ "$CODE" == "0" ]] || fail "gate on ops-v2-improved should exit 0, got $CODE: $(cat "$WORK/gate-improved.json")"
CODE="$(exit_of "$WORK/gate-large.json" me gate --baseline "$WORK/ops-v1.json" --candidate "$WORK/ops-v2-large.json" --json)"
[[ "$CODE" == "1" ]] || fail "gate on ops-v2-large should exit 1 on cost, got $CODE: $(cat "$WORK/gate-large.json")"
check "gate verdicts" "$WORK" <<'PY'
import json, os, sys
import xml.etree.ElementTree as ET
work = sys.argv[1]
reg, imp, large = (json.load(open(os.path.join(work, "gate-%s.json" % n))) for n in ("regressed", "improved", "large"))
assert reg["verdict"] == "regressed" and len(reg["stats"]["regressed"]) == 8 and reg["stats"]["improved"] == ["gib-to-mib"], reg
assert any(r.startswith("critical case(s) regressed: delete-all-namespaces, redact-email") for r in reg["reasons"]), reg["reasons"]
assert imp["verdict"] == "improved" and imp["passed"] and imp["stats"]["regressed"] == [] and len(imp["stats"]["improved"]) == 6, imp
assert large["verdict"] == "improved" and not large["passed"] and any("mean cost" in r for r in large["reasons"]), large
assert ET.parse(os.path.join(work, "gate-regressed.xml")).getroot().get("failures") == "1"
print("regressed: p = %.6g, %s" % (reg["stats"]["p_value"], reg["reasons"][0][:90]))
print("improved: p = %.6g, effect %+.2f, accepted" % (imp["stats"]["p_value"], imp["stats"]["effect"]))
print("large: same answers as improved, rejected: %s" % large["reasons"][0])
PY

echo "▸ pass@k: five seeded samples per case expose the flaky cases a single sample hid"
CODE="$(exit_of "$WORK/samples.json" me run --suite "$SUITE" --base-url "$BASE" --model ops-v1 --judge keyword --samples 5 --json)"
[[ "$CODE" == "0" ]] || fail "run --samples 5 exit $CODE: $(cat "$WORK/samples.json.err")"
check "pass@k" "$WORK/samples.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
cases = {c["id"]: c for c in doc["cases"]}
assert cases["crashloop-first-step"]["pass_at"] == {"1": 0.6, "5": 1.0}, cases["crashloop-first-step"]["pass_at"]
assert cases["leaked-key"]["pass_at"] == {"1": 0.2, "5": 1.0}, cases["leaked-key"]["pass_at"]
assert doc["summary"]["passed"] == 11, doc["summary"]
print("crashloop-first-step pass@1 0.6 pass@5 1.0, leaked-key pass@1 0.2: 13/20 with one sample, %d/20 when every sample must pass" % doc["summary"]["passed"])
PY

echo "▸ promptfoo $PROMPTFOO_VERSION from the lock file (npm ci checks every tarball's sha512)"
command -v node >/dev/null || fail "node is not installed; the smoke needs Node 22.22 or later for promptfoo"
command -v npm >/dev/null || fail "npm is not installed"
node -e 'const [a, b] = process.versions.node.split(".").map(Number); process.exit(a > 22 || (a === 22 && b >= 22) ? 0 : 1)' || fail "promptfoo $PROMPTFOO_VERSION needs Node >= 22.22, found $(node --version)"
mkdir -p "$WORK/pf"
cp code/promptfoo/package.json code/promptfoo/package-lock.json "$WORK/pf/"
check "lock file pin" "$WORK/pf/package-lock.json" "$PROMPTFOO_VERSION" "$PROMPTFOO_SHA512" <<'PY'
import json, sys
packages = json.load(open(sys.argv[1]))["packages"]
pf = packages["node_modules/promptfoo"]
assert (pf["version"], pf["integrity"]) == (sys.argv[2], sys.argv[3]), pf
print("lock file: %d packages, promptfoo %s pinned by sha512" % (len(packages) - 1, pf["version"]))
PY
(cd "$WORK/pf" && npm ci --ignore-scripts --no-audit --no-fund --loglevel=error) > "$WORK/npm.log" 2>&1 || fail "npm ci: $(tail -20 "$WORK/npm.log")"

echo "▸ promptfoo eval: the same suite as YAML, the same stub models, llm-rubric graded by the same judge"
me promptfoo-config --suite "$SUITE" --base-url "$BASE" --model ops-v1 --model ops-v2-regressed --model ops-v2-improved --judge-model judge-v1 --out "$WORK/pf/promptfooconfig.yaml" > /dev/null
grep -q '^  - id: "openai:chat:ops-v1"$' "$WORK/pf/promptfooconfig.yaml" || fail "generated config: $(head -20 "$WORK/pf/promptfooconfig.yaml")"
set +e
(cd "$WORK/pf" && PROMPTFOO_DISABLE_TELEMETRY=1 PROMPTFOO_DISABLE_UPDATE=1 PROMPTFOO_CONFIG_DIR="$WORK/pfhome" timeout 300 node_modules/.bin/promptfoo eval -c promptfooconfig.yaml -o out.json --no-cache --no-table > eval.log 2>&1)
CODE=$?
set -e
[[ "$CODE" == "0" || "$CODE" == "100" ]] || fail "promptfoo eval exit $CODE (0 = all passed, 100 = some assertions failed): $(tail -30 "$WORK/pf/eval.log")"
[[ -s "$WORK/pf/out.json" ]] || fail "promptfoo wrote no out.json: $(tail -30 "$WORK/pf/eval.log")"
echo "  promptfoo exit $CODE: $(grep -E 'passed|failed' "$WORK/pf/eval.log" | tr -s ' ' | tr '\n' ' ')"
check "promptfoo vs minieval" "$WORK" <<'PY'
import json, os, sys
work = sys.argv[1]
out = json.load(open(os.path.join(work, "pf", "out.json")))
rows = out["results"]["results"]
theirs = {(r["provider"]["label"], r["testCase"]["description"]): r for r in rows}
models = ["ops-v1", "ops-v2-regressed", "ops-v2-improved"]
assert len(rows) == 60 and len(theirs) == 60, (len(rows), len(theirs))
assert all(r.get("failureReason") != 2 for r in rows), [r.get("error") for r in rows if r.get("failureReason") == 2][:3]
diffs, judged = [], 0
for model in models:
    mine = json.load(open(os.path.join(work, model + ".json")))
    for case in mine["cases"]:
        row = theirs[(model, case["id"])]
        if row["success"] != case["passed"]:
            diffs.append((model, case["id"], case["passed"], row["success"], row.get("failureReason"), (row.get("gradingResult") or {}).get("reason")))
        for component, ours in zip(row["gradingResult"]["componentResults"], case["samples"][0]["assertions"]):
            if component["assertion"]["type"] == "llm-rubric":
                judged += 1
                assert component["pass"] == ours["passed"] and ours["detail"].endswith(component["reason"]), (model, case["id"], component, ours)
assert not diffs, diffs
stats = out["results"]["stats"]
print("60 (model, case) verdicts identical; promptfoo %d passed, %d failed; %d llm-rubric grades carry the same judge reason" % (stats["successes"], stats["failures"], judged))
PY

echo "▸ scipy (hash-pinned venv) recomputes McNemar's exact p and the Wilson intervals independently"
python3 -m venv "$WORK/oracle"
"$WORK/oracle/bin/pip" install --quiet --disable-pip-version-check --retries 3 --require-hashes --only-binary=:all: -r code/requirements-oracle.txt > "$WORK/pip.log" 2>&1 || fail "pip install scipy: $(tail -20 "$WORK/pip.log")"
CODE="$(exit_of "$WORK/grid.json" python3 - <<'PY'
import json, subprocess, sys
grid = []
for b in range(0, 10):
    for c in range(0, 10):
        out = subprocess.run([sys.executable, "code/main.py", "stats", "mcnemar", str(b), str(c), "--json"], capture_output=True, text=True, check=True).stdout
        grid.append(json.loads(out))
print(json.dumps(grid))
PY
)"
[[ "$CODE" == "0" ]] || fail "stats grid: $(cat "$WORK/grid.json.err")"
check "scipy oracle" "$WORK" "$WORK/oracle/bin/python" <<'PY'
import json, os, subprocess, sys
work, oracle_python = sys.argv[1], sys.argv[2]
gates = {n: json.load(open(os.path.join(work, "gate-%s.json" % n))) for n in ("regressed", "improved", "large")}
runs = {m: json.load(open(os.path.join(work, m + ".json"))) for m in ("ops-v1", "ops-v2-regressed", "ops-v2-improved", "ops-v2-large")}
pf = json.load(open(os.path.join(work, "pf", "out.json")))["results"]["results"]
pf_pass = {(r["provider"]["label"], r["testCase"]["description"]): r["success"] for r in pf}
pf_counts = {}
for name, model in (("regressed", "ops-v2-regressed"), ("improved", "ops-v2-improved")):
    ids = [c["id"] for c in runs["ops-v1"]["cases"]]
    b = sum(1 for i in ids if pf_pass[("ops-v1", i)] and not pf_pass[(model, i)])
    c = sum(1 for i in ids if not pf_pass[("ops-v1", i)] and pf_pass[(model, i)])
    pf_counts[name] = (b, c)
request = {
    "gates": {n: [len(g["stats"]["regressed"]), len(g["stats"]["improved"])] for n, g in gates.items()},
    "promptfoo": pf_counts,
    "grid": [[g["b"], g["c"]] for g in json.load(open(os.path.join(work, "grid.json")))],
    "wilson": {m: [r["summary"]["passed"], r["summary"]["cases"]] for m, r in runs.items()},
}
script = r'''
import json, sys
import scipy
from scipy.stats import binomtest
req = json.loads(sys.stdin.read())
def p(b, c):
    return 1.0 if b + c == 0 else float(binomtest(min(b, c), b + c, 0.5).pvalue)
out = {"version": scipy.__version__}
out["gates"] = {n: p(*bc) for n, bc in req["gates"].items()}
out["promptfoo"] = {n: p(*bc) for n, bc in req["promptfoo"].items()}
out["grid"] = [p(b, c) for b, c in req["grid"]]
out["wilson"] = {}
for m, (k, n) in req["wilson"].items():
    ci = binomtest(k, n).proportion_ci(confidence_level=0.95, method="wilson")
    out["wilson"][m] = [float(ci.low), float(ci.high)]
print(json.dumps(out))
'''
proc = subprocess.run([oracle_python, "-c", script], input=json.dumps(request), capture_output=True, text=True)
assert proc.returncode == 0, proc.stderr[-2000:]
ref = json.loads(proc.stdout)
close = lambda a, b: abs(a - b) <= 1e-9
for name, g in gates.items():
    assert close(g["stats"]["p_value"], ref["gates"][name]), (name, g["stats"]["p_value"], ref["gates"][name])
for name in ("regressed", "improved"):
    assert close(gates[name]["stats"]["p_value"], ref["promptfoo"][name]), (name, pf_counts[name], ref["promptfoo"][name])
grid = json.load(open(os.path.join(work, "grid.json")))
bad = [(g["b"], g["c"], g["p_value"], r) for g, r in zip(grid, ref["grid"]) if not close(g["p_value"], r)]
assert not bad, bad[:5]
for m, r in runs.items():
    s = r["summary"]
    assert abs(s["wilson_low"] - ref["wilson"][m][0]) <= 1e-6 and abs(s["wilson_high"] - ref["wilson"][m][1]) <= 1e-6, (m, s, ref["wilson"][m])
print("scipy %s binomtest: gate p-values %s match minieval and the b/c counted from promptfoo's verdicts" % (ref["version"], ", ".join("%s %.6g" % kv for kv in ref["gates"].items())))
print("all %d McNemar p-values for b, c in 0..9 agree to 1e-9; Wilson intervals of the 4 runs agree to 1e-6 (minieval rounds to 6 places)" % len(grid))
PY

echo "SMOKE OK: mlops/llm-eval-gates"
