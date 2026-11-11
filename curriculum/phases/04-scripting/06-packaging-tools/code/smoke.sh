#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
LESSON="$PWD"
BUILD_VERSION="1.6.1"
BUILD_SHA256="ecd351a4be9d35a9eaaba244a7687143c9c7d4aea6ac964e7e7ddab20cbcf4e7"
PACKAGING_VERSION="26.3"
PACKAGING_SHA256="d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c"
PYPROJECT_HOOKS_VERSION="1.3.3"
PYPROJECT_HOOKS_SHA256="5fc53fdac9f7bd63fbcdc868fb5f90b4784d78a53a3d3388cd738b807441a20b"
FLIT_CORE_VERSION="4.1.0"
FLIT_CORE_SHA256="17398cdd2c38b24047a5a9c93089ec5c0bf12ec3d1469bbf69c27ed7965299db"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

command -v git >/dev/null || fail "needs git"
PY="$(command -v python3)" || fail "needs python3"
"$PY" -c 'import sys, venv; assert sys.version_info >= (3, 9), sys.version' || fail "needs python3 >= 3.9 with venv"
shipit() { "$PY" "$LESSON/code/main.py" "$@"; }
unset SOURCE_DATE_EPOCH PYTHONPATH PIP_REQUIRE_VIRTUALENV || true
export GIT_CONFIG_GLOBAL="$WORK/gitconfig" GIT_CONFIG_NOSYSTEM=1
git config --global user.name "Release Bot"
git config --global user.email "release-bot@example.invalid"
git config --global init.defaultBranch main
git config --global commit.gpgsign false
git config --global tag.gpgsign false
export GIT_AUTHOR_DATE="2026-11-01T10:00:00+00:00" GIT_COMMITTER_DATE="2026-11-01T10:00:00+00:00"
REPO="$WORK/repo"
INDEX="$WORK/index"
LOCK="$WORK/shipit.lock"
WHEEL_NAME="cc_envcheck-1.2.0-py3-none-any.whl"

echo "▸ the sample tool in a git repository tagged v1.2.0"
cp -R code/sample "$REPO"
git -C "$REPO" init -q
git -C "$REPO" add -A
git -C "$REPO" commit -q -m "feat: first release"
git -C "$REPO" tag -a v1.2.0 -m "v1.2.0"
shipit version --project "$REPO" --json > "$WORK/version.json" 2> "$WORK/version.err" || fail "shipit version: $(cat "$WORK/version.err")"
"$PY" - "$WORK/version.json" <<'PY' || fail "version from the tag: $(cat "$WORK/version.json")"
import json, sys
v = json.load(open(sys.argv[1]))
assert (v["version"], v["tag"], v["distance"], v["dirty"]) == ("1.2.0", "v1.2.0", 0, False), v
PY
[[ "$(git -C "$REPO" describe --tags)" == "v1.2.0" ]] || fail "git describe disagrees: $(git -C "$REPO" describe --tags)"
echo "  shipit version: 1.2.0 from tag v1.2.0 (git describe: $(git -C "$REPO" describe --tags --long))"

echo "▸ fixture index: shipit builds the dependency wheels and every RECORD verifies"
shipit index code/fixtures/index.json --out "$INDEX" --quiet || fail "shipit index"
WHEELS=("$INDEX"/*.whl)
[[ "${#WHEELS[@]}" == "8" ]] || fail "expected 8 fixture wheels, got ${#WHEELS[@]}: ${WHEELS[*]}"
for wheel in "${WHEELS[@]}"; do
  shipit inspect --quiet "$wheel" || fail "shipit inspect $(basename "$wheel")"
done
echo "  ${#WHEELS[@]} wheels in $(basename "$INDEX")/"

echo "▸ lock: highest allowed versions, pre-releases skipped, every pin hashed"
shipit lock --project "$REPO" --find-links "$INDEX" --out "$LOCK" > "$WORK/lock.txt" || fail "shipit lock"
cat "$WORK/lock.txt"
"$PY" - "$LOCK" "$INDEX" <<'PY' || fail "lock contents: $(cat "$LOCK")"
import hashlib, re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
pins = dict(re.findall(r"^([a-z0-9-]+)==(\S+) \\$", text, re.M))
assert pins == {"cc-colors": "1.1.0", "cc-term": "0.3.1"}, pins
for name, version in pins.items():
    wheel = Path(sys.argv[2]) / f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert f"--hash=sha256:{digest}" in text, (name, digest)
PY

echo "▸ build twice from the same commit: byte-identical wheels"
shipit build --project "$REPO" --out "$WORK/d1" --json > "$WORK/build1.json" 2> "$WORK/build1.err" || fail "shipit build: $(cat "$WORK/build1.err")"
shipit build --project "$REPO" --out "$WORK/d2" --quiet || fail "second shipit build"
WHEEL="$WORK/d1/$WHEEL_NAME"
[[ -f "$WHEEL" ]] || fail "expected $WHEEL_NAME, got: $(ls "$WORK/d1")"
cmp "$WHEEL" "$WORK/d2/$WHEEL_NAME" || fail "two builds of the same commit differ"
SOURCE_DATE_EPOCH=1767225600 shipit build --project "$REPO" --out "$WORK/d3" --quiet || fail "build with SOURCE_DATE_EPOCH"
cmp -s "$WHEEL" "$WORK/d3/$WHEEL_NAME" && fail "a different SOURCE_DATE_EPOCH should change the zip timestamps"
echo "  sha256 $(sha256sum "$WHEEL" | cut -c1-16)... twice; a different SOURCE_DATE_EPOCH gives $(sha256sum "$WORK/d3/$WHEEL_NAME" | cut -c1-16)..."

echo "▸ Use It: python -m build with flit_core, pinned and hash-checked in a venv"
"$PY" -m venv "$WORK/tools"
cat > "$WORK/tools-requirements.txt" <<EOF
build==$BUILD_VERSION --hash=sha256:$BUILD_SHA256
packaging==$PACKAGING_VERSION --hash=sha256:$PACKAGING_SHA256
pyproject-hooks==$PYPROJECT_HOOKS_VERSION --hash=sha256:$PYPROJECT_HOOKS_SHA256
flit-core==$FLIT_CORE_VERSION --hash=sha256:$FLIT_CORE_SHA256
EOF
"$WORK/tools/bin/pip" install --quiet --disable-pip-version-check --retries 3 --require-hashes --only-binary=:all: -r "$WORK/tools-requirements.txt" > "$WORK/pip-tools.txt" 2>&1 || fail "pip install build tools: $(cat "$WORK/pip-tools.txt")"
"$WORK/tools/bin/python" -m build --no-isolation --outdir "$WORK/dist-build" "$REPO" > "$WORK/build.txt" 2>&1 || fail "python -m build: $(cat "$WORK/build.txt")"
ls "$WORK/dist-build"
[[ -f "$WORK/dist-build/$WHEEL_NAME" ]] || fail "python -m build should name the wheel $WHEEL_NAME, got: $(ls "$WORK/dist-build")"
"$WORK/tools/bin/python" - "$WHEEL" "$WORK/dist-build/$WHEEL_NAME" "$REPO/pyproject.toml" "$LESSON/code" <<'PY' || fail "shipit's wheel does not match the one python -m build made"
import configparser, sys, tomllib, zipfile
from packaging.metadata import Metadata
from packaging.utils import parse_wheel_filename
from pathlib import Path
sys.path.insert(0, sys.argv[4])
import main
mine, theirs = Path(sys.argv[1]), Path(sys.argv[2])
assert parse_wheel_filename(mine.name) == parse_wheel_filename(theirs.name), (mine.name, theirs.name)
def load(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        info = next(n.split("/")[0] for n in names if n.endswith(".dist-info/METADATA"))
        meta = Metadata.from_email(z.read(f"{info}/METADATA"), validate=True)
        eps = configparser.ConfigParser(delimiters=("=",))
        eps.optionxform = str
        eps.read_string(z.read(f"{info}/entry_points.txt").decode())
        code = {n: z.read(n) for n in names if not n.split("/")[0].endswith(".dist-info")}
        return meta, dict(eps["console_scripts"]), code
m, m_eps, m_code = load(mine)
t, t_eps, t_code = load(theirs)
fields = ["metadata_version", "name", "version", "summary", "requires_python", "license_expression", "author_email", "keywords", "classifiers", "project_urls", "description_content_type", "import_names"]
for f in fields:
    assert getattr(m, f) == getattr(t, f), (f, getattr(m, f), getattr(t, f))
assert sorted(map(str, m.requires_dist)) == sorted(map(str, t.requires_dist)), (m.requires_dist, t.requires_dist)
assert m.description.strip() == t.description.strip(), "description bodies differ"
assert m_eps == t_eps, (m_eps, t_eps)
assert m_code == t_code, (sorted(m_code), sorted(t_code))
assert main.parse_toml(Path(sys.argv[3]).read_text()) == tomllib.loads(Path(sys.argv[3]).read_text()), "shipit's TOML subset disagrees with tomllib"
print(f"  same file name, {len(fields) + 2} METADATA fields (Metadata-Version {m.metadata_version}), console scripts and {len(m_code)} package files")
PY

SOURCE_DATE_EPOCH=1767225600 "$WORK/tools/bin/python" -m build --no-isolation --wheel --outdir "$WORK/flit1" "$REPO" > "$WORK/flit1.txt" 2>&1 || fail "python -m build: $(cat "$WORK/flit1.txt")"
SOURCE_DATE_EPOCH=1767225600 "$WORK/tools/bin/python" -m build --no-isolation --wheel --outdir "$WORK/flit2" "$REPO" > "$WORK/flit2.txt" 2>&1 || fail "python -m build: $(cat "$WORK/flit2.txt")"
cmp "$WORK/flit1/$WHEEL_NAME" "$WORK/flit2/$WHEEL_NAME" || fail "flit_core should be reproducible with SOURCE_DATE_EPOCH set"
echo "  flit_core is byte-reproducible too once SOURCE_DATE_EPOCH is set"

echo "▸ pip installs shipit's wheel into a fresh venv and its console script runs"
"$PY" -m venv "$WORK/app"
"$WORK/app/bin/pip" install --quiet --disable-pip-version-check --no-index --find-links "$INDEX" --require-hashes -r "$LOCK" > "$WORK/pip-deps.txt" 2>&1 || fail "pip install -r shipit.lock: $(cat "$WORK/pip-deps.txt")"
"$WORK/app/bin/pip" install --quiet --disable-pip-version-check --no-index --no-deps "$WHEEL" > "$WORK/pip-app.txt" 2>&1 || fail "pip install shipit's wheel: $(cat "$WORK/pip-app.txt")"
"$WORK/app/bin/pip" check > "$WORK/pip-check.txt" 2>&1 || fail "pip check: $(cat "$WORK/pip-check.txt")"
[[ "$("$WORK/app/bin/cc-envcheck" --version)" == "cc-envcheck 1.2.0" ]] || fail "console script --version: $("$WORK/app/bin/cc-envcheck" --version 2>&1)"
set +e
DEPLOY_ENV=prod "$WORK/app/bin/cc-envcheck" --json DEPLOY_ENV CC_SMOKE_UNSET > "$WORK/check.json"
CODE=$?
set -e
[[ "$CODE" == "1" ]] || fail "cc-envcheck should exit 1 when a variable is missing, got $CODE"
"$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["missing"]==["CC_SMOKE_UNSET"], d' "$WORK/check.json" || fail "cc-envcheck --json: $(cat "$WORK/check.json")"
"$WORK/app/bin/pip" show -f cc-envcheck > "$WORK/show.txt" || fail "pip show -f"
"$PY" - "$WORK/show.txt" "$WHEEL" <<'PY' || fail "pip show -f does not list shipit's RECORD: $(cat "$WORK/show.txt")"
import csv, io, sys, zipfile
text = open(sys.argv[1]).read()
fields = dict(line.split(": ", 1) for line in text.splitlines() if ": " in line and not line.startswith(" "))
assert fields["Version"] == "1.2.0" and fields["Requires"] == "cc-colors", fields
listed = {line.strip() for line in text.split("Files:", 1)[1].splitlines() if line.strip()}
with zipfile.ZipFile(sys.argv[2]) as z:
    record = [row[0] for row in csv.reader(io.StringIO(z.read("cc_envcheck-1.2.0.dist-info/RECORD").decode())) if row]
missing = [path for path in record if path not in listed]
assert not missing, missing
extra = sorted(listed - set(record))
print(f"  pip show -f lists all {len(record)} RECORD paths plus what pip added: {', '.join(e for e in extra if '__pycache__' not in e)}")
PY

echo "▸ a tampered dependency with a consistent RECORD: pip and shipit both refuse"
"$PY" - "$LESSON/code/fixtures/index.json" "$WORK/evil.json" <<'PY'
import json, sys
spec = json.load(open(sys.argv[1]))
for package in spec["packages"]:
    if package["name"] == "cc-term" and package["version"] == "0.3.1":
        package["files"]["cc_term/__init__.py"] += "\nprint('exfiltrating credentials')\n"
json.dump(spec, open(sys.argv[2], "w"))
PY
shipit index "$WORK/evil.json" --out "$WORK/evil-index" --quiet
shipit inspect --quiet "$WORK/evil-index/cc_term-0.3.1-py3-none-any.whl" || fail "the tampered wheel's own RECORD should still verify"
"$PY" -m venv "$WORK/victim"
set +e
"$WORK/victim/bin/pip" install --disable-pip-version-check --no-index --find-links "$WORK/evil-index" --require-hashes -r "$LOCK" > "$WORK/pip-evil.txt" 2>&1
PIP_CODE=$?
shipit install --lock "$LOCK" --find-links "$WORK/evil-index" --target "$WORK/evil-target" > /dev/null 2> "$WORK/shipit-evil.txt"
SHIPIT_CODE=$?
set -e
[[ "$PIP_CODE" != "0" ]] || fail "pip installed a wheel whose hash is not in the lock"
grep -qi "do not match the hashes" "$WORK/pip-evil.txt" || fail "pip should report a hash mismatch: $(cat "$WORK/pip-evil.txt")"
"$WORK/victim/bin/python" -c 'import cc_term' 2>/dev/null && fail "pip left cc_term installed"
[[ "$SHIPIT_CODE" == "1" ]] || fail "shipit install should exit 1 on a hash mismatch, got $SHIPIT_CODE: $(cat "$WORK/shipit-evil.txt")"
grep -q "hash mismatch" "$WORK/shipit-evil.txt" || fail "shipit should name the mismatch: $(cat "$WORK/shipit-evil.txt")"
[[ ! -e "$WORK/evil-target" ]] || fail "shipit wrote files before verifying hashes: $(find "$WORK/evil-target")"
echo "  pip: $(grep -m1 -i "expected sha256" "$WORK/pip-evil.txt" | sed 's/^ *//')"
echo "  shipit: $(grep -m1 "hash mismatch" "$WORK/shipit-evil.txt" | sed 's/^ *//' | cut -c1-110)"

echo "▸ shipit install: verify, install into a target directory, and a re-run changes nothing"
shipit install --lock "$LOCK" --find-links "$INDEX" "$WHEEL" --target "$WORK/target" --json > "$WORK/install1.json" || fail "shipit install"
shipit install --lock "$LOCK" --find-links "$INDEX" "$WHEEL" --target "$WORK/target" --json > "$WORK/install2.json" || fail "second shipit install"
"$PY" - "$WORK/install1.json" "$WORK/install2.json" <<'PY' || fail "install actions: $(cat "$WORK/install1.json" "$WORK/install2.json")"
import json, sys
first, second = (json.load(open(path)) for path in sys.argv[1:3])
assert [(r["name"], r["action"]) for r in first] == [("cc-colors", "installed"), ("cc-term", "installed"), ("cc-envcheck", "installed")], first
assert {r["action"] for r in second} == {"unchanged"}, second
PY
[[ "$(env -i PATH="$(dirname "$PY"):/usr/bin:/bin" "$WORK/target/bin/cc-envcheck" --version)" == "cc-envcheck 1.2.0" ]] || fail "the installed script does not run"

echo "▸ zipapp: one file with the locked dependencies inside, run by a bare python3"
shipit zipapp --project "$REPO" --lock "$LOCK" --find-links "$INDEX" --out "$WORK/cc-envcheck.pyz" --quiet || fail "shipit zipapp"
shipit zipapp --project "$REPO" --lock "$LOCK" --find-links "$INDEX" --out "$WORK/again.pyz" --quiet || fail "second shipit zipapp"
cmp "$WORK/cc-envcheck.pyz" "$WORK/again.pyz" || fail "two zipapp builds differ"
[[ "$(head -c 22 "$WORK/cc-envcheck.pyz")" == "#!/usr/bin/env python3" ]] || fail "zipapp shebang: $(head -c 40 "$WORK/cc-envcheck.pyz")"
OUT="$(env -i "$PY" "$WORK/cc-envcheck.pyz" --version)" || fail "python3 cc-envcheck.pyz: $OUT"
[[ "$OUT" == "cc-envcheck 1.2.0" ]] || fail "zipapp --version: $OUT"
OUT="$(cd / && env -i PATH="$(dirname "$PY"):/usr/bin:/bin" HOME=/tmp "$WORK/cc-envcheck.pyz" --json HOME)" || fail "./cc-envcheck.pyz: $OUT"
[[ "$OUT" == '{"checked": ["HOME"], "missing": []}' ]] || fail "zipapp run through its shebang: $OUT"
mkdir "$WORK/stage"
cp -R "$WORK/target/." "$WORK/stage/"
rm -rf "$WORK/stage/bin"
"$PY" -m zipapp "$WORK/stage" -m "cc_envcheck.cli:main" -p "/usr/bin/env python3" -o "$WORK/stdlib.pyz" || fail "python -m zipapp"
[[ "$(env -i "$PY" "$WORK/stdlib.pyz" --version)" == "cc-envcheck 1.2.0" ]] || fail "python -m zipapp build does not run"
echo "  $(du -k "$WORK/cc-envcheck.pyz" | cut -f1) KiB, byte-identical on rebuild; python -m zipapp over the installed tree runs the same"

echo "▸ after the tag: a dev version that sorts between 1.2.0 and 1.2.1"
echo "Run it in CI before every deploy." >> "$REPO/README.md"
GIT_AUTHOR_DATE="2026-11-02T10:00:00+00:00" GIT_COMMITTER_DATE="2026-11-02T10:00:00+00:00" git -C "$REPO" commit -q -am "docs: CI usage"
DEV="$(shipit version --project "$REPO" 2> "$WORK/dev.err")"
SHORT="$(git -C "$REPO" rev-parse --short=7 HEAD)"
[[ "$DEV" == "1.2.1.dev1+g$SHORT" ]] || fail "expected 1.2.1.dev1+g$SHORT, got $DEV"
grep -q "__version__ is 1.2.0 but the git tag gives $DEV" "$WORK/dev.err" || fail "shipit should warn that __version__ disagrees: $(cat "$WORK/dev.err")"
"$WORK/tools/bin/python" -c 'import sys; from packaging.version import Version as V; v=V(sys.argv[1]); assert V("1.2.0") < v < V("1.2.1"), v; assert str(v) == sys.argv[1], v' "$DEV" || fail "packaging does not order $DEV between 1.2.0 and 1.2.1"
echo "local edit" >> "$REPO/README.md"
DIRTY="$(shipit version --project "$REPO" 2>/dev/null)"
[[ "$DIRTY" == "1.2.1.dev1+g$SHORT.dirty" ]] || fail "a dirty tree should be marked, got $DIRTY"
echo "  $DEV after one commit, $DIRTY with an uncommitted edit"

echo "SMOKE OK: scripting/packaging-tools"
