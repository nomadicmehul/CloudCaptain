#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux"
command -v python3 >/dev/null || fail "needs python3"
command -v curl >/dev/null || fail "needs curl to download the pinned ShellCheck"

WORK="$(mktemp -d)"
cleanup() { rm -rf "${WORK:?}"; }
trap cleanup EXIT

SC_VERSION="v0.11.0"
case "$(uname -m)" in
  x86_64) SC_ARCH="x86_64"; SC_SHA256="b7af85e41cc99489dcc21d66c6d5f3685138f06d34651e6d34b42ec6d54fe6f6" ;;
  aarch64|arm64) SC_ARCH="aarch64"; SC_SHA256="68a8133197a50beb8803f8d42f9908d1af1c5540d4bb05fdfca8c1fa47decefc" ;;
  *) fail "no pinned ShellCheck build for $(uname -m)" ;;
esac
LESSON="$PWD"
BL=(python3 code/main.py)
FIX=code/fixtures

echo "▸ bashlint: the checks and the ShellCheck codes they mirror"
"${BL[@]}" checks | tee "$WORK/checks.txt"
[[ "$(wc -l < "$WORK/checks.txt")" -eq 13 ]] || fail "expected 13 checks, got: $(cat "$WORK/checks.txt")"

echo "▸ download ShellCheck $SC_VERSION ($SC_ARCH) and verify its sha256"
TARBALL="$WORK/shellcheck-$SC_VERSION.linux.$SC_ARCH.tar.gz"
curl -fsSL --retry 3 -o "$TARBALL" "https://github.com/koalaman/shellcheck/releases/download/$SC_VERSION/shellcheck-$SC_VERSION.linux.$SC_ARCH.tar.gz"
echo "$SC_SHA256  $TARBALL" > "$WORK/sha256.txt"
sha256sum -c "$WORK/sha256.txt" > "$WORK/sha256.out" 2>&1 || fail "checksum mismatch: $(cat "$WORK/sha256.out")"
mkdir -p "$WORK/bin"
tar -xzf "$TARBALL" -C "$WORK" "shellcheck-$SC_VERSION/shellcheck"
mv "$WORK/shellcheck-$SC_VERSION/shellcheck" "$WORK/bin/shellcheck"
SC=("$WORK/bin/shellcheck" --enable=useless-use-of-cat)
"${SC[0]}" --version > "$WORK/scversion.txt"
grep -q "^version: ${SC_VERSION#v}$" "$WORK/scversion.txt" || fail "unexpected ShellCheck version: $(cat "$WORK/scversion.txt")"
echo "  $(grep '^version:' "$WORK/scversion.txt"), sha256 verified"

echo "▸ planted problems: bashlint and ShellCheck on $FIX/bad"
set +e
"${BL[@]}" check --json "$FIX/bad" > "$WORK/bl-bad.json" 2> "$WORK/bl-bad.err"
BL_CODE=$?
"${SC[@]}" -f json1 "$FIX"/bad/*.sh > "$WORK/sc-bad.json" 2> "$WORK/sc-bad.err"
SC_CODE=$?
set -e
[[ "$BL_CODE" == "1" ]] || fail "bashlint should exit 1 on the bad fixtures, got $BL_CODE: $(cat "$WORK/bl-bad.err")"
[[ "$SC_CODE" == "1" ]] || fail "shellcheck should exit 1 on the bad fixtures, got $SC_CODE: $(cat "$WORK/sc-bad.err")"
python3 - "$FIX/expected.json" "$WORK/bl-bad.json" "$WORK/sc-bad.json" code/main.py <<'PY' || fail "planted problems are not reported by both tools"
import json, os, sys
expected = json.load(open(sys.argv[1]))
bashlint = json.load(open(sys.argv[2]))
shellcheck = json.load(open(sys.argv[3]))["comments"]
sys.path.insert(0, os.path.dirname(sys.argv[4]))
sys.dont_write_bytecode = True
import main
mapping = {c.id: c.shellcheck for c in main.CHECKS}
bl = {(os.path.basename(r["file"]), r["line"], r["id"]): r["column"] for r in bashlint}
sc = {}
for c in shellcheck:
    sc.setdefault((os.path.basename(c["file"]), c["line"], "SC%d" % c["code"]), c["column"])
problems, rows, same_column = [], 0, 0
for name in sorted(expected):
    for item in expected[name]:
        key = (name, item["line"], item["id"])
        code = mapping[item["id"]]
        seen_bl = key in bl
        seen_sc = (name, item["line"], code) in sc if code else None
        if not seen_bl:
            problems.append(f"bashlint missed {key}")
        if code and not seen_sc:
            problems.append(f"shellcheck did not report {code} at {name}:{item['line']} ({item['id']})")
        if code and seen_sc and seen_bl and sc[(name, item["line"], code)] == bl[key]:
            same_column += 1
        rows += 1
        print(f"  {name}:{item['line']:<3} {item['id']}  {code or '(no ShellCheck code)':<20} bashlint={'yes' if seen_bl else 'NO'} shellcheck={'-' if code is None else 'yes' if seen_sc else 'NO'}")
extra = sorted(set(bl) - {(n, i["line"], i["id"]) for n, items in expected.items() for i in items})
if extra:
    problems.append(f"bashlint reported findings that were not planted: {extra}")
mapped = sum(1 for n, items in expected.items() for i in items if mapping[i["id"]])
print(f"  {rows} planted problems; {mapped} have a ShellCheck code and both tools report them, {same_column} at the same column")
others = sorted({"SC%d" % c["code"] for c in shellcheck} - set(mapping.values()))
print(f"  ShellCheck also reported: {', '.join(others) or 'nothing else'}")
if problems:
    print("\n".join(problems), file=sys.stderr)
    sys.exit(1)
PY

echo "▸ fixed versions: clean for both tools"
"${BL[@]}" check --json "$FIX/good" > "$WORK/bl-good.json" 2> "$WORK/bl-good.err" || fail "bashlint flagged a fixed fixture: $(cat "$WORK/bl-good.json")"
"${SC[@]}" -f json1 "$FIX"/good/*.sh > "$WORK/sc-good.json" || fail "shellcheck flagged a fixed fixture: $(cat "$WORK/sc-good.json")"
python3 - "$WORK/bl-good.json" "$WORK/sc-good.json" <<'PY' || fail "fixed fixtures are not clean"
import json, sys
bl = json.load(open(sys.argv[1]))
sc = json.load(open(sys.argv[2]))["comments"]
assert bl == [] and sc == [], (bl, sc)
print("  bashlint: 0 findings, shellcheck: 0 comments")
PY

echo "▸ code/main.sh passes ShellCheck and bashlint"
"${SC[@]}" -f gcc code/main.sh > "$WORK/sc-main.txt" || fail "shellcheck on code/main.sh: $(cat "$WORK/sc-main.txt")"
"${BL[@]}" check code/main.sh > "$WORK/bl-main.txt" 2>&1 || fail "bashlint on code/main.sh: $(cat "$WORK/bl-main.txt")"
bash -n code/main.sh || fail "bash -n code/main.sh"
echo "  clean"

echo "▸ the by-hand checks in code/main.sh are a subset of bashlint's"
set +e
bash code/main.sh "$FIX"/bad/*.sh > "$WORK/byhand.txt" 2> "$WORK/byhand.err"
HAND_CODE=$?
set -e
[[ "$HAND_CODE" == "1" ]] || fail "main.sh should exit 1 on the bad fixtures, got $HAND_CODE: $(cat "$WORK/byhand.err")"
cat "$WORK/byhand.txt"
python3 - "$WORK/byhand.txt" "$WORK/bl-bad.json" <<'PY' || fail "main.sh reported something bashlint did not"
import json, os, sys
hand = set()
for line in open(sys.argv[1]):
    path, number, rest = line.split(":", 2)
    hand.add((os.path.basename(path), int(number), rest.split()[0]))
bl = {(os.path.basename(r["file"]), r["line"], r["id"]) for r in json.load(open(sys.argv[2]))}
assert hand and hand <= bl, sorted(hand - bl)
print(f"  {len(hand)} of bashlint's {len(bl)} findings are visible to grep")
PY
bash code/main.sh "$FIX"/good/*.sh > "$WORK/byhand-good.txt" 2>&1 || fail "main.sh flagged a fixed fixture: $(cat "$WORK/byhand-good.txt")"
set +e
bash code/main.sh --no-such-flag > "$WORK/usage.txt" 2>&1
USAGE_CODE=$?
set -e
[[ "$USAGE_CODE" == "2" ]] || fail "an unknown flag should exit 2, got $USAGE_CODE: $(cat "$WORK/usage.txt")"

echo "▸ why it matters: the planted deploy.sh against a path with a space"
mkdir -p "$WORK/src" "$WORK/rel dir" "$WORK/target bad" "$WORK/target good" "$WORK/cwd"
echo "v2" > "$WORK/src/app.txt"
tar -czf "$WORK/rel dir/release 2.tgz" -C "$WORK/src" app.txt
echo "keep me private" > "$WORK/cwd/canary.txt"
chmod 0600 "$WORK/cwd/canary.txt"
mode() { python3 -c 'import os, sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777))' "$1"; }
set +e
(cd "$WORK/cwd" && bash "$LESSON/$FIX/bad/deploy.sh" "$WORK/rel dir/release 2.tgz" "$WORK/target bad") > "$WORK/bad-deploy.txt" 2>&1
BAD_CODE=$?
(cd "$WORK/cwd" && bash "$LESSON/$FIX/good/deploy.sh" "$WORK/rel dir/release 2.tgz" "$WORK/target good") > "$WORK/good-deploy.txt" 2>&1
GOOD_CODE=$?
(cd "$WORK/cwd" && bash "$LESSON/$FIX/good/deploy.sh") > "$WORK/usage-deploy.txt" 2>&1
NOARG_CODE=$?
set -e
sed 's/^/  bad:  /' "$WORK/bad-deploy.txt"
[[ "$BAD_CODE" == "0" ]] || fail "the bad deploy.sh should report success (that is the bug), got $BAD_CODE"
[[ ! -e "$WORK/target bad/app.txt" ]] || fail "the bad deploy.sh should not have deployed anything"
[[ "$(mode "$WORK/cwd/canary.txt")" == "0o644" ]] || fail "the bad deploy.sh should have chmodded the canary in the wrong directory, mode is $(mode "$WORK/cwd/canary.txt")"
echo "  bad:  exit 0, nothing deployed, and chmod 0644 ran on canary.txt in the caller's directory"
chmod 0600 "$WORK/cwd/canary.txt"
[[ "$GOOD_CODE" == "0" ]] || fail "the fixed deploy.sh failed: $(cat "$WORK/good-deploy.txt")"
[[ "$(cat "$WORK/target good/app.txt")" == "v2" ]] || fail "the fixed deploy.sh did not deploy app.txt"
[[ "$(mode "$WORK/cwd/canary.txt")" == "0o600" ]] || fail "the fixed deploy.sh touched the caller's directory"
echo "  good: exit 0, app.txt deployed into 'target good', canary untouched"
[[ "$NOARG_CODE" != "0" ]] || fail "the fixed deploy.sh should refuse to run without arguments"
grep -q "usage: deploy.sh" "$WORK/usage-deploy.txt" || fail "missing usage message: $(cat "$WORK/usage-deploy.txt")"
echo "  good without arguments: exit $NOARG_CODE, $(head -1 "$WORK/usage-deploy.txt" | sed 's/^.*: //')"

echo "▸ bash -x with a PS4 that names file, line and function"
TRACE_PS4='+ ${BASH_SOURCE##*/}:${LINENO}:${FUNCNAME[0]:-main}: '
if [[ "$(id -u)" == "0" ]]; then
  echo "  (running as root: bash does not import PS4 from the environment, so it is set inside the shell)"
  bash -c 'PS4="$1"; shift; set -x; source "$@"' trace '+ ${BASH_SOURCE[0]-bash}:${LINENO}:${FUNCNAME[0]:-main}: ' code/main.sh "$FIX/good/deploy.sh" > "$WORK/trace.out" 2> "$WORK/trace.txt" || fail "traced run failed: $(tail -5 "$WORK/trace.txt")"
else
  PS4="$TRACE_PS4" bash -x code/main.sh "$FIX/good/deploy.sh" > "$WORK/trace.out" 2> "$WORK/trace.txt" || fail "traced run failed: $(tail -5 "$WORK/trace.txt")"
fi
grep -E '^\++ ([^ ]*/)?main\.sh:[0-9]+:' "$WORK/trace.txt" > "$WORK/trace-lines.txt" || fail "no PS4-formatted trace lines: $(head -5 "$WORK/trace.txt")"
head -8 "$WORK/trace-lines.txt" | sed 's/^/  /'
echo "  ..."
grep -E ':cleanup: rm -rf -- ' "$WORK/trace-lines.txt" | sed 's/^/  /' || fail "the EXIT trap did not run cleanup"
grep -qE ':lint_file: grep -Eq ' "$WORK/trace-lines.txt" || fail "trace does not show lint_file"
echo "  $(wc -l < "$WORK/trace-lines.txt") traced commands; the EXIT trap removed the work directory"

echo "SMOKE OK: scripting/robust-bash"
