#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
MARKER="2026-11-01T10:20:00Z"

echo "▸ generate 20k synthetic lines: 30 templates, a deploy at minute 20 that introduces a new ERROR and a WARN spike"
python3 code/main.py --generate 20000 --seed 7 > "$WORK/app.log" 2> "$WORK/generate.err"
LINES=$(wc -l < "$WORK/app.log")
[[ "$LINES" -eq 20000 ]] || fail "expected 20000 lines, got $LINES"
grep -q "deploy marker $MARKER" "$WORK/generate.err" || fail "generator did not print the marker: $(cat "$WORK/generate.err")"
JSON_LINES=$(grep -c '^{' "$WORK/app.log" || true)
[[ "$JSON_LINES" -gt 1000 ]] || fail "expected a mix of JSON and plain lines, got $JSON_LINES JSON lines"

echo "▸ mine them: the novel ERROR ranks first, the digest fits the budget, the token ratio is > 20x"
python3 code/main.py "$WORK/app.log" --marker "$MARKER" --budget 2000 --no-model --json > "$WORK/mined.json"
python3 - "$WORK/mined.json" <<'PY' || fail "digest assertions"
import json, sys
r = json.load(open(sys.argv[1]))
a, d = r["analysis"], r["digest"]
top = a["ranked"][0]
assert top["novel"] and top["level"] == "ERROR" and "DATABASE_URL" in top["template"], top
assert 25 <= a["templates"] <= 60, a["templates"]
assert d["tokens"] <= 2000, d["tokens"]
assert r["ratio"] > 20, r["ratio"]
spike = next(x for x in a["ranked"] if "upstream timeout" in x["template"])
assert spike["spike"] and spike["spike"] > 3, spike
assert "eyJ" not in d["text"], "secrets must not reach the digest"
assert r["explanation"] is None
print("  lines", a["lines"], "templates", a["templates"], "digest tokens", d["tokens"], "ratio %.0fx" % r["ratio"], "top:", top["id"], top["template"])
PY
python3 code/main.py "$WORK/app.log" --marker "$MARKER" --no-model > "$WORK/digest.txt"
head -12 "$WORK/digest.txt"

echo "▸ a digest with every template, so the token-bearing auth line is in it: its tokens are redacted"
python3 code/main.py "$WORK/app.log" --marker "$MARKER" --top 40 --budget 8000 --no-model --json > "$WORK/all.json"
python3 - "$WORK/all.json" <<'PY' || fail "redaction assertions"
import json, sys
d = json.load(open(sys.argv[1]))["digest"]
assert "eyJ" not in d["text"], "a session token reached the digest"
assert "token=<REDACTED>" in d["text"], "the auth template should show token=<REDACTED>"
print("  digest with every template:", d["tokens"], "tokens, session tokens redacted")
PY
grep -q "^T01  score .* ERROR NEW" "$WORK/digest.txt" || fail "T01 should be the NEW error in the text digest"
grep -q "x smaller" "$WORK/digest.txt" || fail "token report missing"

echo "▸ the same digest explained by a scripted model (no API key needed)"
env -u ANTHROPIC_API_KEY python3 code/main.py "$WORK/app.log" --marker "$MARKER" --replay code/fixtures/replay-deploy.json --json > "$WORK/replay.json"
python3 - "$WORK/replay.json" <<'PY' || fail "replay assertions"
import json, sys
e = json.load(open(sys.argv[1]))["explanation"]
assert e["stop"] == "report" and e["report"]["severity"] == "sev2", e["stop"]
assert e["unknown_refs"] == [], e["unknown_refs"]
assert e["cost_usd"] > 0, e
print("  stop", e["stop"], "severity", e["report"]["severity"], "evidence", e["report"]["evidence"], "cost $%.4f" % e["cost_usd"])
PY
env -u ANTHROPIC_API_KEY python3 code/main.py "$WORK/app.log" --marker "$MARKER" --replay code/fixtures/replay-deploy.json | grep -q "EXPLANATION \[sev2\] confidence high" || fail "human-readable explanation missing"

echo "▸ without a key, without --replay and without --no-model the tool says what to do"
set +e
env -u ANTHROPIC_API_KEY python3 code/main.py "$WORK/app.log" --marker "$MARKER" > /dev/null 2> "$WORK/nokey.err"
CODE=$?
set -e
[[ "$CODE" == "1" ]] || fail "expected exit 1 without a key, got $CODE"
grep -q "ANTHROPIC_API_KEY" "$WORK/nokey.err" || fail "error should name the variable: $(cat "$WORK/nokey.err")"

if command -v kubectl >/dev/null && kubectl cluster-info >/dev/null 2>&1; then
  echo "▸ Use It: real kubectl logs from the cluster (control plane, all containers, prefixed, with timestamps), mined as-is"
  kubectl logs -n kube-system -l tier=control-plane --all-containers --prefix --timestamps --since=1h --tail=300 > "$WORK/kube.log" 2> /dev/null || true
  if [[ -s "$WORK/kube.log" ]]; then
    python3 code/main.py "$WORK/kube.log" --no-model --top 5 --budget 1500 > "$WORK/kube-digest.txt"
    head -30 "$WORK/kube-digest.txt"
    python3 code/main.py "$WORK/kube.log" --no-model --json > "$WORK/kube.json"
    python3 -c 'import json,sys; a=json.load(open(sys.argv[1]))["analysis"]; assert a["lines"]>0 and a["templates"]>0; print("  kube-system:", a["lines"], "lines ->", a["templates"], "templates; sources seen:", bool(any(r["sources"] for r in a["ranked"])))' "$WORK/kube.json" || fail "kube logs did not mine"
  else
    echo "  (no control-plane logs returned; skipped)"
  fi
else
  echo "  (no reachable cluster; kubectl logs part skipped)"
fi

if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "▸ live explanation with the model (ANTHROPIC_API_KEY is set)"
  python3 code/main.py "$WORK/app.log" --marker "$MARKER" --json > "$WORK/live.json" || true
  python3 -c 'import json,sys; e=json.load(open(sys.argv[1]))["explanation"]; assert e["stop"] in ("report","text"), e["stop"]; print("  live stop", e["stop"], "cost $%.4f" % e["cost_usd"])' "$WORK/live.json" || fail "live run"
else
  echo "  (ANTHROPIC_API_KEY not set; live run skipped)"
fi

echo "SMOKE OK: aiops/log-analysis"
