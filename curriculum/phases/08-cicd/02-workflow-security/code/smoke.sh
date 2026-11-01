#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WFAUDIT="$PWD/code/main.py"
FIXTURES="$PWD/code/fixtures"
EXPECTED="$FIXTURES/expected.json"
ACTIONLINT_VERSION="1.7.12"
ZIZMOR_VERSION="1.31.1"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
mkdir -p "$WORK/bin" "$WORK/dl"
export PATH="$WORK/bin:$PATH"
unset GH_TOKEN GITHUB_TOKEN ZIZMOR_GITHUB_TOKEN || true

sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }

cat > "$WORK/check_wfaudit.py" <<'PY'
import json
import os
import sys

expected_path, report_path, mode = sys.argv[1:4]
with open(expected_path) as handle:
    planted = {(p["file"], p["line"], p["rule"]) for p in json.load(handle)["planted"]}
with open(report_path) as handle:
    report = json.load(handle)
got = {(os.path.basename(f["file"]), f["line"], f["rule"]) for f in report["findings"]}
if mode == "fixed":
    if got:
        sys.exit(f"wfaudit reported findings on the fixed workflows: {sorted(got)}")
    print(f"  wfaudit: 0 findings on {report['summary']['workflows']} fixed workflows")
    sys.exit(0)
if got != planted:
    sys.exit(f"wfaudit disagrees with expected.json\n  missing: {sorted(planted - got)}\n  extra: {sorted(got - planted)}")
counts = report["summary"]["by_severity"]
print(f"  wfaudit: {len(got)} findings, all planted ({counts['critical']} critical, {counts['high']} high, {counts['medium']} medium)")
PY

cat > "$WORK/check_sarif.py" <<'PY'
import json
import sys

sarif_path, report_path = sys.argv[1:3]
with open(sarif_path) as handle:
    sarif = json.load(handle)
with open(report_path) as handle:
    report = json.load(handle)
assert sarif["version"] == "2.1.0", sarif["version"]
run = sarif["runs"][0]
rules = [rule["id"] for rule in run["tool"]["driver"]["rules"]]
results = run["results"]
assert len(results) == report["summary"]["findings"], (len(results), report["summary"]["findings"])
for result in results:
    assert rules[result["ruleIndex"]] == result["ruleId"], result
    assert result["locations"][0]["physicalLocation"]["region"]["startLine"] >= 1, result
print(f"  SARIF 2.1.0: {len(rules)} rules, {len(results)} results")
PY

cat > "$WORK/check_zizmor.py" <<'PY'
import json
import os
import sys


def path_in(node):
    if isinstance(node, str) and node.endswith((".yml", ".yaml")):
        return node
    if isinstance(node, dict):
        for value in node.values():
            found = path_in(value)
            if found:
                return found
    return None


expected_path, report_path, mode = sys.argv[1:4]
with open(report_path) as handle:
    findings = json.load(handle)
spans = {}
for finding in findings:
    for location in finding.get("locations", []):
        path = path_in(location.get("symbolic", {}).get("key"))
        point = location.get("concrete", {}).get("location", {})
        if not path or "start_point" not in point:
            continue
        span = (point["start_point"]["row"] + 1, point["end_point"]["row"] + 1)
        spans.setdefault((os.path.basename(path), finding["ident"]), []).append(span)
if mode == "fixed":
    if findings:
        shown = sorted({(os.path.basename(path_in(f["locations"][0]["symbolic"]["key"]) or "?"), f["ident"], f["determinations"]["severity"]) for f in findings})
        sys.exit(f"zizmor reported {len(findings)} finding(s) on the fixed workflows: {shown}")
    print("  zizmor: 0 findings on the fixed workflows")
    sys.exit(0)


def covered(item, ident):
    return any(start - 1 <= item["line"] <= end for start, end in spans.get((item["file"], ident), []))


with open(expected_path) as handle:
    planted = json.load(handle)["planted"]
missing, matched, also = [], {}, []
for item in planted:
    if item.get("zizmor"):
        if covered(item, item["zizmor"]):
            matched[item["zizmor"]] = matched.get(item["zizmor"], 0) + 1
        else:
            missing.append((item["file"], item["line"], item["rule"], item["zizmor"]))
    if item.get("zizmor_also") and covered(item, item["zizmor_also"]):
        also.append(f"{item['file']}:{item['line']} {item['zizmor_also']}")
if missing:
    sys.exit(f"zizmor did not report these planted findings on the same line: {missing}\n  zizmor spans: {sorted(spans.items())}")
for ident in ("unpinned-uses", "template-injection", "dangerous-triggers", "excessive-permissions"):
    if not matched.get(ident):
        sys.exit(f"no overlap for {ident}: {matched}")
print(f"  zizmor: {len(findings)} findings; every planted one marked for zizmor is on the same line: {dict(sorted(matched.items()))}")
if also:
    print(f"  zizmor also reports: {', '.join(also)}")
PY

cat > "$WORK/check_actionlint.py" <<'PY'
import json
import os
import sys

expected_path, report_path, mode = sys.argv[1:4]
with open(report_path) as handle:
    text = handle.read()
errors = (json.loads(text) if text.strip() else None) or []
if mode == "fixed":
    if errors:
        sys.exit("actionlint reported errors on the fixed workflows: " + "; ".join(f"{os.path.basename(e.get('filepath', '?'))}:{e.get('line')} {e.get('message')} [{e.get('kind')}]" for e in errors))
    print("  actionlint: 0 errors on the fixed workflows")
    sys.exit(0)
untrusted = [(os.path.basename(e.get("filepath", "")), e.get("line")) for e in errors if e.get("kind") == "expression" and "potentially untrusted" in e.get("message", "")]
with open(expected_path) as handle:
    planted = [p for p in json.load(handle)["planted"] if p.get("actionlint")]
missing = [(p["file"], p["line"]) for p in planted if (p["file"], p["line"]) not in untrusted and (p["file"], p["actionlint"]) not in untrusted]
if missing:
    sys.exit(f"actionlint did not flag these injections: {missing}; it flagged {untrusted}")
print(f"  actionlint: {len(errors)} errors; the {len(planted)} planted injections from its untrusted-input list are flagged: {sorted(untrusted)}")
PY

echo "▸ wfaudit on the fixtures: exactly the planted findings, and the fixed twins are clean"
code=0
python3 "$WFAUDIT" --public --json "$FIXTURES/vulnerable" > "$WORK/wfaudit-vulnerable.json" 2> "$WORK/wfaudit.err" || code=$?
[[ "$code" == "1" ]] || fail "wfaudit on the vulnerable fixtures exited $code, want 1: $(cat "$WORK/wfaudit.err")"
python3 "$WORK/check_wfaudit.py" "$EXPECTED" "$WORK/wfaudit-vulnerable.json" vulnerable || fail "wfaudit planted-findings comparison"
python3 "$WFAUDIT" --public --json "$FIXTURES/fixed" > "$WORK/wfaudit-fixed.json" 2> "$WORK/wfaudit.err" || fail "wfaudit on the fixed fixtures should exit 0: $(cat "$WORK/wfaudit-fixed.json" "$WORK/wfaudit.err")"
python3 "$WORK/check_wfaudit.py" "$EXPECTED" "$WORK/wfaudit-fixed.json" fixed || fail "wfaudit fixed comparison"
python3 "$WFAUDIT" --public --sarif "$FIXTURES/vulnerable" > "$WORK/wfaudit.sarif" || true
python3 "$WORK/check_sarif.py" "$WORK/wfaudit.sarif" "$WORK/wfaudit-vulnerable.json" || fail "wfaudit SARIF: $(head -c 2000 "$WORK/wfaudit.sarif")"
python3 "$WFAUDIT" --public --min-severity critical "$FIXTURES/vulnerable" > "$WORK/critical.txt" || true
head -1 "$WORK/critical.txt"
grep -q "11 finding(s): 11 critical" "$WORK/critical.txt" || fail "critical-only summary: $(head -3 "$WORK/critical.txt")"

echo "▸ two Git repositories with the workflows under .github/workflows"
for kind in vulnerable fixed; do
  repo="$WORK/repo-$kind"
  mkdir -p "$repo/.github/workflows"
  cp "$FIXTURES/$kind/"*.yml "$repo/.github/workflows/"
  git -C "$repo" init -q
  git -C "$repo" -c user.name=smoke -c user.email=smoke@example.invalid add .
  git -C "$repo" -c user.name=smoke -c user.email=smoke@example.invalid commit -q -m "Add workflows"
done
code=0
python3 "$WFAUDIT" --public --json "$WORK/repo-vulnerable" > "$WORK/wfaudit-repo.json" 2> "$WORK/wfaudit.err" || code=$?
[[ "$code" == "1" ]] || fail "wfaudit on the vulnerable repository exited $code: $(cat "$WORK/wfaudit.err")"
python3 "$WORK/check_wfaudit.py" "$EXPECTED" "$WORK/wfaudit-repo.json" vulnerable || fail "wfaudit on the repository root"
python3 "$WFAUDIT" --public "$WORK/repo-fixed" > "$WORK/wfaudit-repo-fixed.txt" || fail "wfaudit on the fixed repository: $(cat "$WORK/wfaudit-repo-fixed.txt")"

echo "▸ download actionlint v$ACTIONLINT_VERSION and verify its sha256"
case "$(uname -s)" in
  Linux) AL_OS=linux ;;
  Darwin) AL_OS=darwin ;;
  *) fail "unsupported OS $(uname -s)" ;;
esac
case "$(uname -m)" in
  x86_64|amd64) AL_ARCH=amd64 ;;
  arm64|aarch64) AL_ARCH=arm64 ;;
  *) fail "unsupported architecture $(uname -m)" ;;
esac
AL_ASSET="actionlint_${ACTIONLINT_VERSION}_${AL_OS}_${AL_ARCH}.tar.gz"
AL_BASE="https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}"
curl -fsSL --retry 3 -o "$WORK/dl/$AL_ASSET" "$AL_BASE/$AL_ASSET" || fail "download $AL_BASE/$AL_ASSET"
curl -fsSL --retry 3 -o "$WORK/dl/actionlint-checksums.txt" "$AL_BASE/actionlint_${ACTIONLINT_VERSION}_checksums.txt" || fail "download the actionlint checksums"
WANT="$(awk -v f="$AL_ASSET" '$2 == f {print $1}' "$WORK/dl/actionlint-checksums.txt")"
GOT="$(sha256_of "$WORK/dl/$AL_ASSET")"
[[ -n "$WANT" && "$WANT" == "$GOT" ]] || fail "actionlint checksum mismatch: want '$WANT' got '$GOT'"
tar -xzf "$WORK/dl/$AL_ASSET" -C "$WORK/bin" actionlint
chmod +x "$WORK/bin/actionlint"
actionlint -version > "$WORK/actionlint-version.txt"
head -1 "$WORK/actionlint-version.txt"
if command -v shellcheck >/dev/null; then
  echo "  shellcheck $(shellcheck --version | awk '/^version:/ {print $2}') found; actionlint checks run: scripts with it"
else
  echo "  (shellcheck not installed; actionlint's shellcheck integration skipped)"
fi

echo "▸ install zizmor $ZIZMOR_VERSION from PyPI into a venv, with the wheel hashes pinned"
cat > "$WORK/zizmor-requirements.txt" <<EOF
zizmor==${ZIZMOR_VERSION} \\
    --hash=sha256:58e878eb2dbd2c6183c105e1ac28c14f7dd1eca44a783f1fe2a69647d012c410 \\
    --hash=sha256:d9b36840c560a8abe39d9ef3d077239aabf6d572c5c2b629c970592a617dbf6f \\
    --hash=sha256:e7fdf82d31cc67eb1d5030bf2372b25ee2a1c3ad05017e18da70ed138c4accd6 \\
    --hash=sha256:c5ccb17fd820bb1ddc63cc9a715e97c063025487e585f640389a86d9dfc97cda \\
    --hash=sha256:9696d3c10919a3a2eb086c102f1bc10717f53ef3700a0d6c55a709c274995476 \\
    --hash=sha256:4a70ee3c2721122aff04d5bbad5e448d15384f45de2ecb3c9493fe9dac2bc95f
EOF
python3 -m venv "$WORK/venv" || fail "python3 -m venv"
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check --only-binary=:all: --require-hashes -r "$WORK/zizmor-requirements.txt" > "$WORK/pip.log" 2>&1 || fail "pip install zizmor: $(tail -20 "$WORK/pip.log")"
ln -s "$WORK/venv/bin/zizmor" "$WORK/bin/zizmor"
zizmor --version > "$WORK/zizmor-version.txt" 2>&1 || fail "zizmor --version: $(cat "$WORK/zizmor-version.txt")"
head -1 "$WORK/zizmor-version.txt"

echo "▸ zizmor on the vulnerable repository: the overlapping classes on the same files and lines"
(cd "$WORK/repo-vulnerable" && zizmor --offline --no-exit-codes --format json .github/workflows/*.yml) > "$WORK/zizmor-vulnerable.json" 2> "$WORK/zizmor.err" || fail "zizmor on the vulnerable repository: $(tail -20 "$WORK/zizmor.err")"
python3 "$WORK/check_zizmor.py" "$EXPECTED" "$WORK/zizmor-vulnerable.json" vulnerable || fail "zizmor comparison"
(cd "$WORK/repo-vulnerable" && zizmor --offline --format sarif .github/workflows/*.yml) > "$WORK/zizmor.sarif" 2> "$WORK/zizmor.err" || fail "zizmor --format sarif: $(tail -20 "$WORK/zizmor.err")"
python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); n = len(d["runs"][0]["results"]); assert d["version"] == "2.1.0" and n > 0, n; print(f"  zizmor SARIF 2.1.0: {n} results")' "$WORK/zizmor.sarif" || fail "zizmor SARIF: $(head -c 2000 "$WORK/zizmor.sarif")"

echo "▸ zizmor on the fixed repository: no findings"
(cd "$WORK/repo-fixed" && zizmor --offline --no-exit-codes --format json .github/workflows/*.yml) > "$WORK/zizmor-fixed.json" 2> "$WORK/zizmor.err" || fail "zizmor on the fixed repository: $(tail -20 "$WORK/zizmor.err")"
python3 "$WORK/check_zizmor.py" "$EXPECTED" "$WORK/zizmor-fixed.json" fixed || fail "zizmor fixed comparison"

echo "▸ actionlint on the vulnerable repository: the injections it knows as untrusted inputs"
code=0
(cd "$WORK/repo-vulnerable" && actionlint -format '{{json .}}') > "$WORK/actionlint-vulnerable.json" 2> "$WORK/actionlint.err" || code=$?
[[ "$code" == "1" ]] || fail "actionlint on the vulnerable repository exited $code, want 1: $(cat "$WORK/actionlint.err" "$WORK/actionlint-vulnerable.json")"
python3 "$WORK/check_actionlint.py" "$EXPECTED" "$WORK/actionlint-vulnerable.json" vulnerable || fail "actionlint comparison"

echo "▸ actionlint on the fixed repository: clean"
code=0
(cd "$WORK/repo-fixed" && actionlint -format '{{json .}}') > "$WORK/actionlint-fixed.json" 2> "$WORK/actionlint.err" || code=$?
python3 "$WORK/check_actionlint.py" "$EXPECTED" "$WORK/actionlint-fixed.json" fixed || fail "actionlint fixed comparison (exit $code): $(cat "$WORK/actionlint.err")"
[[ "$code" == "0" ]] || fail "actionlint on the fixed repository exited $code: $(cat "$WORK/actionlint.err" "$WORK/actionlint-fixed.json")"

echo "SMOKE OK: cicd/workflow-security"
