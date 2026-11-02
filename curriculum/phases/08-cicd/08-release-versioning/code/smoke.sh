#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
LESSON="$PWD"
MR="$LESSON/code/main.py"
CLIFF_CONFIG="$LESSON/code/cliff.toml"
FIXTURES="$LESSON/code/fixtures"
GIT_CLIFF_VERSION="2.14.2"
SEMVER_JS_VERSION="7.8.5"
SEMVER_JS_INTEGRITY="sha512-Y7/KDsb8LjooZpwaqGyulO6DQlksgCncchHGk+sZIY4SBvUocMBEFH5Ur1fI4dV+Jvl0w6cjvucaIi40puRioA=="
PACKAGING_VERSION="26.3"
PACKAGING_SHA256="d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
mkdir -p "$WORK/bin" "$WORK/dl"
export PATH="$WORK/bin:$PATH"
export GIT_CONFIG_GLOBAL="$WORK/gitconfig" GIT_CONFIG_NOSYSTEM=1
git config --global user.name "Release Bot"
git config --global user.email "release-bot@example.invalid"
git config --global init.defaultBranch main
git config --global commit.gpgsign false
git config --global tag.gpgsign false

sha_of() { if command -v "sha$1sum" >/dev/null; then "sha$1sum" "$2" | awk '{print $1}'; else shasum -a "$1" "$2" | awk '{print $1}'; fi; }

cat > "$WORK/sections.py" <<'EOF'
import json
import re
import sys


def from_markdown(path):
    releases, version, section = {}, None, None
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip()
            heading = re.match(r"## \[([^\]]+)\]", line)
            if heading:
                version, section = heading.group(1), None
                releases[version] = {}
                continue
            sub = re.match(r"### (.+)", line)
            if sub and version:
                section = sub.group(1).strip()
                releases[version].setdefault(section, set())
                continue
            if line.startswith("- ") and version and section:
                releases[version][section].add(line[2:].strip())
    return {v: {s: e for s, e in secs.items() if e} for v, secs in releases.items() if any(secs.values())}


def from_cliff_context(path):
    releases = {}
    with open(path, encoding="utf-8") as handle:
        for release in json.load(handle):
            version = (release.get("version") or "Unreleased").lstrip("v")
            sections = {}
            for commit in release.get("commits") or []:
                group = re.sub(r"<!--.*?-->", "", commit.get("group") or "").strip()
                text = ("**BREAKING** " if commit.get("breaking") else "") + (f"**{commit['scope']}:** " if commit.get("scope") else "") + commit["message"].split("\n")[0].strip()
                sections.setdefault(group, set()).add(text)
            if sections:
                releases[version] = sections
    return releases


def from_minirelease_json(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return {(r["version"] or "Unreleased"): {s: {e[2:] for e in entries} for s, entries in r["sections"].items()} for r in data if r["sections"]}


def show(releases):
    return json.dumps({v: {s: sorted(e) for s, e in secs.items()} for v, secs in releases.items()}, indent=1, sort_keys=True)


LOADERS = {"md": from_markdown, "context": from_cliff_context, "mini": from_minirelease_json}
kind_a, path_a, kind_b, path_b = sys.argv[1:5]
a, b = LOADERS[kind_a](path_a), LOADERS[kind_b](path_b)
if a != b:
    sys.exit(f"changelogs differ\n--- {path_a}\n{show(a)}\n--- {path_b}\n{show(b)}")
if len(sys.argv) > 5:
    expected = {v: {s: set(e) for s, e in secs.items()} for v, secs in json.loads(sys.argv[5]).items()}
    if a != expected:
        sys.exit(f"both tools agree but not with the expected changelog\n{show(a)}")
print(f"  same sections and entries: {', '.join(f'{v} ({sum(len(e) for e in s.values())})' for v, s in a.items())}")
EOF

EXPECTED_CHANGELOG='{
  "Unreleased": {
    "Added": ["**BREAKING** **api:** rename /items to /inventory"],
    "Changed": ["**db:** batch inserts"],
    "Deprecated": ["deprecate the v1 export format"],
    "Removed": ["**BREAKING** drop support for Python 3.8"],
    "Fixed": ["close the database pool on shutdown"],
    "Security": ["**security:** escape item names in log lines"]
  },
  "0.2.0": {"Added": ["**cli:** add --json output"], "Fixed": ["**api:** return 404 for unknown items"]},
  "0.1.0": {"Added": ["add the inventory endpoint"]}
}'

echo "▸ minirelease deps on the fixture tree proposes exactly the planted updates"
code=0
python3 "$MR" deps "$FIXTURES/deps" --available "$FIXTURES/available.json" --json > "$WORK/deps.json" 2> "$WORK/deps.err" || code=$?
[[ "$code" == 1 ]] || fail "deps should exit 1 when it proposes updates, got $code: $(cat "$WORK/deps.err")"
python3 - "$WORK/deps.json" <<'PY' || fail "deps proposals: $(head -c 3000 "$WORK/deps.json")"
import json, sys
planted = {
    ("major", ".github/workflows/ci.yml", "actions/checkout", "v5"),
    ("major", ".github/workflows/ci.yml", "docker/build-push-action", "v7.0.0"),
    ("major", "requirements.txt", "urllib3", "2.2.3"),
    ("major", "requirements.txt", "gunicorn", "23.0.0"),
    ("major", "web/package.json", "express", "5.1.0"),
    ("major", "web/package.json", "tiny-lib", "0.5.0"),
    ("major", "web/package.json", "chalk", "5.4.1"),
    ("major", "web/package.json", "eslint", "9.17.0"),
    ("major", "web/package.json", "prettier", "3.4.2"),
    ("minor", ".github/workflows/ci.yml", "actions/setup-python", "v5.6.0"),
    ("minor", ".github/workflows/ci.yml", "docker/build-push-action", "v6.18.0"),
    ("minor", ".github/workflows/ci.yml", "alpine", "3.22"),
    ("minor", "Dockerfile", "python", "3.13-slim"),
    ("minor", "Dockerfile", "nginx", "1.29.3-alpine"),
    ("minor", "requirements.txt", "requests", "2.32.3"),
    ("minor", "requirements.txt", "flask", "3.1.1"),
    ("minor", "web/package.json", "express", "4.21.2"),
    ("minor", "web/package.json", "zod", "3.24.1"),
    ("minor", "web/package.json", "chalk", "4.1.2"),
    ("minor", "web/package.json", "typescript", "5.6.3"),
    ("minor", "web/package.json", "eslint", "8.57.0"),
    ("patch", "requirements.txt", "urllib3", "1.26.20"),
    ("patch", "requirements.txt", "pyyaml", "6.0.2"),
    ("patch", "web/package.json", "lodash", "4.17.21"),
    ("patch", "web/package.json", "tiny-lib", "0.4.3"),
    ("pin", "Dockerfile", "busybox", "1.37.0"),
    ("pin", "requirements.txt", "structlog", "25.1.0"),
}
report = json.load(open(sys.argv[1]))
got = {(u["group"], u["file"], u["name"], u["to_version"]) for u in report["updates"]}
assert got == planted, f"missing {sorted(planted - got)}\nextra {sorted(got - planted)}"
skipped = sorted(d["name"] for d in report["dependencies"] if d["skip"])
assert skipped == ["./.github/actions/lint", "build", "example-org/deploy-action", "left-pad", "python", "scratch"], skipped
print(f"  {len(got)} planted updates found, {len(skipped)} dependencies skipped with a reason")
PY

echo "▸ download git-cliff v$GIT_CLIFF_VERSION and verify its published sha512"
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64|Linux-amd64) TARGET=x86_64-unknown-linux-musl ;;
  Linux-aarch64|Linux-arm64) TARGET=aarch64-unknown-linux-musl ;;
  Darwin-arm64) TARGET=aarch64-apple-darwin ;;
  Darwin-x86_64) TARGET=x86_64-apple-darwin ;;
  *) fail "unsupported platform $(uname -s)-$(uname -m)" ;;
esac
ASSET="git-cliff-${GIT_CLIFF_VERSION}-${TARGET}.tar.gz"
BASE="https://github.com/orhun/git-cliff/releases/download/v${GIT_CLIFF_VERSION}"
curl -fsSL --retry 3 -o "$WORK/dl/$ASSET" "$BASE/$ASSET" || fail "download $BASE/$ASSET"
curl -fsSL --retry 3 -o "$WORK/dl/$ASSET.sha512" "$BASE/$ASSET.sha512" || fail "download $BASE/$ASSET.sha512"
WANT="$(awk '{print $1; exit}' "$WORK/dl/$ASSET.sha512")"
GOT="$(sha_of 512 "$WORK/dl/$ASSET")"
[[ -n "$WANT" && "$WANT" == "$GOT" ]] || fail "git-cliff checksum mismatch: want '$WANT' got '$GOT'"
tar -xzf "$WORK/dl/$ASSET" -C "$WORK/dl"
install -m 0755 "$WORK/dl/git-cliff-${GIT_CLIFF_VERSION}/git-cliff" "$WORK/bin/git-cliff"
git-cliff --version > "$WORK/cliff-version.txt" 2>&1 || fail "git-cliff --version: $(cat "$WORK/cliff-version.txt")"
grep -q "$GIT_CLIFF_VERSION" "$WORK/cliff-version.txt" || fail "unexpected git-cliff version: $(cat "$WORK/cliff-version.txt")"
cat "$WORK/cliff-version.txt"

echo "▸ build a repository with two tags and a scripted Conventional Commits history"
REPO="$WORK/repo"
mkdir -p "$REPO"
git -C "$REPO" init --quiet
STAMP=1790000000
commit() {
  STAMP=$((STAMP + 3600))
  echo "$STAMP" >> "$REPO/app.txt"
  git -C "$REPO" add app.txt
  GIT_AUTHOR_DATE="$STAMP +0000" GIT_COMMITTER_DATE="$STAMP +0000" git -C "$REPO" commit --quiet "$@"
}
tag() { GIT_COMMITTER_DATE="$STAMP +0000" git -C "$REPO" tag -a "$1" -m "$1"; }
commit -m "feat: add the inventory endpoint"
tag v0.1.0
commit -m "fix(api): return 404 for unknown items"
commit -m "docs: describe the endpoints"
commit -m "feat(cli): add --json output"
tag v0.2.0
commit -m "fix: close the database pool on shutdown"
commit -m "feat(api)!: rename /items to /inventory" -m "The old path was ambiguous." -m "BREAKING CHANGE: clients must call /inventory instead of /items"
commit -m "Update README"
commit -m "perf(db): batch inserts"
commit -m "fix(security): escape item names in log lines"
commit -m "feat: deprecate the v1 export format"
commit -m "refactor!: drop support for Python 3.8"
commit -m "chore(deps): bump requests"
git -C "$REPO" log --oneline --decorate > "$WORK/history.txt"
cat "$WORK/history.txt"

echo "▸ minirelease log: every commit classified, the non-conventional one fails --strict"
python3 "$MR" log --repo "$REPO" > "$WORK/log.txt" || fail "minirelease log: $(cat "$WORK/log.txt")"
cat "$WORK/log.txt"
grep -q "8 commit(s) since v0.2.0" "$WORK/log.txt" || fail "log header: $(cat "$WORK/log.txt")"
grep -q "Update README .*not conventional" "$WORK/log.txt" || fail "log should flag the non-conventional commit: $(cat "$WORK/log.txt")"
code=0
python3 "$MR" log --repo "$REPO" --strict > /dev/null || code=$?
[[ "$code" == 1 ]] || fail "log --strict should exit 1 on a non-conventional commit, got $code"

echo "▸ next version: minirelease next == git-cliff --bumped-version, with and without the 0.x rule"
sed -e 's/^features_always_bump_minor = false/features_always_bump_minor = true/' \
    -e 's/^breaking_always_bump_major = false/breaking_always_bump_major = true/' "$CLIFF_CONFIG" > "$WORK/cliff-leave-zero.toml"
grep -q "^breaking_always_bump_major = true" "$WORK/cliff-leave-zero.toml" || fail "could not derive the leave-0.x config"
compare_next() {
  local want="$1" config="$2"; shift 2
  local mine theirs
  mine="$(python3 "$MR" next --repo "$REPO" "$@" 2> "$WORK/next.err")" || fail "minirelease next $*: $(cat "$WORK/next.err")"
  theirs="$(git-cliff --repository "$REPO" --config "$config" --offline --bumped-version 2> "$WORK/cliff-next.err" | tail -n 1)" || fail "git-cliff --bumped-version: $(cat "$WORK/cliff-next.err")"
  [[ "${mine#v}" == "${theirs#v}" ]] || fail "minirelease says $mine, git-cliff says $theirs ($(cat "$WORK/next.err"))"
  [[ "$mine" == "$want" ]] || fail "both tools say $mine, expected $want"
  echo "  $mine from both tools ($(cat "$WORK/next.err"))"
}
compare_next v0.3.0 "$CLIFF_CONFIG"
compare_next v1.0.0 "$WORK/cliff-leave-zero.toml" --breaking-always-bump-major --features-always-bump-minor
PRE="$(python3 "$MR" next --repo "$REPO" --pre rc 2> /dev/null)" || fail "minirelease next --pre rc"
[[ "$PRE" == "v0.3.0-rc.1" ]] || fail "next --pre rc: $PRE"

echo "▸ the whole changelog: minirelease and git-cliff give the same sections and entries"
python3 "$MR" changelog --repo "$REPO" > "$WORK/mine.md" || fail "minirelease changelog: $(cat "$WORK/mine.md")"
git-cliff --repository "$REPO" --config "$CLIFF_CONFIG" --offline > "$WORK/cliff.md" 2> "$WORK/cliff.err" || fail "git-cliff: $(cat "$WORK/cliff.err")"
python3 "$WORK/sections.py" md "$WORK/mine.md" md "$WORK/cliff.md" "$EXPECTED_CHANGELOG" || fail "changelog comparison"
git-cliff --repository "$REPO" --config "$CLIFF_CONFIG" --offline --context > "$WORK/context.json" 2> "$WORK/cliff.err" || fail "git-cliff --context: $(cat "$WORK/cliff.err")"
python3 "$MR" changelog --repo "$REPO" --json > "$WORK/mine.json" || fail "minirelease changelog --json"
python3 "$WORK/sections.py" mini "$WORK/mine.json" context "$WORK/context.json" || fail "git-cliff context against minirelease JSON"

echo "▸ release: changelog commit, then an annotated tag (SSH-signed when ssh-keygen exists)"
SIGN=()
if command -v ssh-keygen >/dev/null; then
  ssh-keygen -q -t ed25519 -N "" -C release-bot -f "$WORK/release_key"
  git -C "$REPO" config gpg.format ssh
  git -C "$REPO" config user.signingkey "$WORK/release_key"
  echo "release-bot@example.invalid $(cat "$WORK/release_key.pub")" > "$WORK/allowed_signers"
  SIGN=(--sign)
else
  echo "  (ssh-keygen not installed; the tag is annotated but not signed)"
fi
STAMP=$((STAMP + 3600))
GIT_AUTHOR_DATE="$STAMP +0000" GIT_COMMITTER_DATE="$STAMP +0000" \
  python3 "$MR" release --repo "$REPO" --changelog CHANGELOG.md --commit ${SIGN[@]+"${SIGN[@]}"} > "$WORK/release.txt" 2>&1 || fail "minirelease release: $(cat "$WORK/release.txt")"
cat "$WORK/release.txt"
[[ "$(git -C "$REPO" cat-file -t v0.3.0)" == "tag" ]] || fail "v0.3.0 is not an annotated tag object"
[[ "$(git -C "$REPO" log -1 --format=%s)" == "chore(release): v0.3.0" ]] || fail "release commit: $(git -C "$REPO" log -1 --format=%s)"
[[ "$(git -C "$REPO" rev-parse 'v0.3.0^{commit}')" == "$(git -C "$REPO" rev-parse HEAD)" ]] || fail "the tag does not point at the release commit"
git -C "$REPO" for-each-ref refs/tags/v0.3.0 --format='%(contents)' > "$WORK/tag-message.txt"
grep -q "^### Security" "$WORK/tag-message.txt" || fail "the tag message lost its headings: $(cat "$WORK/tag-message.txt")"
grep -q "^- clients must call /inventory instead of /items" "$WORK/tag-message.txt" || fail "the tag message has no upgrade note: $(cat "$WORK/tag-message.txt")"
if [[ ${#SIGN[@]} -gt 0 ]]; then
  git -C "$REPO" -c gpg.ssh.allowedSignersFile="$WORK/allowed_signers" tag -v v0.3.0 > "$WORK/verify.txt" 2>&1 || fail "git tag -v: $(cat "$WORK/verify.txt")"
  grep -q 'Good "git" signature' "$WORK/verify.txt" || fail "git tag -v: $(cat "$WORK/verify.txt")"
  echo "  git tag -v: Good \"git\" signature for release-bot@example.invalid"
fi
git-cliff --repository "$REPO" --config "$CLIFF_CONFIG" --offline --latest --context > "$WORK/latest.json" 2> "$WORK/cliff.err" || fail "git-cliff --latest --context: $(cat "$WORK/cliff.err")"
python3 - "$WORK/latest.json" <<'PY' || fail "git-cliff does not see v0.3.0 as the latest release: $(head -c 2000 "$WORK/latest.json")"
import json, sys
releases = json.load(open(sys.argv[1]))
assert releases[0]["version"] == "v0.3.0", releases[0]["version"]
PY
python3 "$MR" changelog --repo "$REPO" --json > "$WORK/after.json" || fail "minirelease changelog --json after the release"
python3 - "$WORK/after.json" "$WORK/after-latest.json" <<'PY'
import json, sys
json.dump([r for r in json.load(open(sys.argv[1])) if r["version"] == "0.3.0"], open(sys.argv[2], "w"))
PY
python3 "$WORK/sections.py" mini "$WORK/after-latest.json" context "$WORK/latest.json" || fail "the tagged release differs between the tools"
git-cliff --repository "$REPO" --config "$CLIFF_CONFIG" --offline > "$WORK/cliff-after.md" 2> "$WORK/cliff.err" || fail "git-cliff after the release: $(cat "$WORK/cliff.err")"
python3 "$WORK/sections.py" md "$REPO/CHANGELOG.md" md "$WORK/cliff-after.md" || fail "the committed CHANGELOG.md differs from git-cliff's"
code=0
python3 "$MR" next --repo "$REPO" > /dev/null 2> "$WORK/next.err" || code=$?
[[ "$code" == 1 ]] && grep -q "nothing to release" "$WORK/next.err" || fail "right after the release next should exit 1 with nothing to release, got $code: $(cat "$WORK/next.err")"

echo "▸ the feature half of the 0.x rule: feat in 0.3.0 gives 0.3.1, or 0.4.0 with features_always_bump_minor"
commit -m "feat: add CSV export"
compare_next v0.3.1 "$CLIFF_CONFIG"
compare_next v0.4.0 "$WORK/cliff-leave-zero.toml" --breaking-always-bump-major --features-always-bump-minor

echo "▸ PEP 440 specifiers and ordering against packaging $PACKAGING_VERSION"
python3 -m venv "$WORK/venv"
echo "packaging==$PACKAGING_VERSION --hash=sha256:$PACKAGING_SHA256" > "$WORK/pip-requirements.txt"
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check --require-hashes -r "$WORK/pip-requirements.txt" > "$WORK/pip.txt" 2>&1 || fail "pip install packaging: $(cat "$WORK/pip.txt")"
"$WORK/venv/bin/python" - "$LESSON/code" "$FIXTURES/available.json" <<'PY' || fail "PEP 440 comparison with packaging"
import json, sys
sys.path.insert(0, sys.argv[1])
import main
from packaging.specifiers import SpecifierSet
from packaging.version import Version
versions = sorted({v for vs in json.load(open(sys.argv[2]))["pypi"].values() for v in vs} | {"1.0.dev0", "1.0a1", "1.0b2", "1.0rc1", "1.0", "1.0.post1", "1.1", "1.4.0", "1.4.9", "1.5.0", "2.0", "2.2", "2.2.5", "2.3", "3.0.0b1", "1!0.1", "1!2.3"})
specs = ["==2.31.0", "==3.0.3", "~=1.26.18", ">=21.2,!=22.0.0,<23", "==6.0.1", "==1.4.*", "<23", ">=2", ">=3.0.0a1", ">1.0", "~=2.2", "!=1.0.post1", ">=1.0,<=1.1", "==0.1.*"]
mine_order = [v.text for v in sorted((main.pep440(v) for v in versions), key=lambda v: v.key)]
theirs_order = sorted(versions, key=Version)
assert [str(Version(v)) for v in mine_order] == [str(Version(v)) for v in theirs_order], (mine_order, theirs_order)
problems = []
for spec in specs:
    reference = SpecifierSet(spec)
    allow = bool(reference.prereleases)
    for v in versions:
        mine = main.pip_satisfies(main.pep440(v), main.pip_specifiers(spec))
        theirs = reference.contains(v, prereleases=allow)
        if mine != theirs:
            problems.append(f"{v!r} in {spec!r}: minirelease {mine}, packaging {theirs}")
assert not problems, "\n".join(problems)
print(f"  {len(versions)} versions sorted identically; {len(specs)} specifiers x {len(versions)} versions agree")
PY

if command -v node >/dev/null; then
  echo "▸ npm ranges and ordering against node-semver $SEMVER_JS_VERSION"
  TARBALL="https://registry.npmjs.org/semver/-/semver-${SEMVER_JS_VERSION}.tgz"
  curl -fsSL --retry 3 -o "$WORK/dl/semver.tgz" "$TARBALL" || fail "download $TARBALL"
  GOT="sha512-$(openssl dgst -sha512 -binary "$WORK/dl/semver.tgz" | openssl base64 -A)"
  [[ "$GOT" == "$SEMVER_JS_INTEGRITY" ]] || fail "semver tarball integrity mismatch: want $SEMVER_JS_INTEGRITY got $GOT"
  mkdir -p "$WORK/semver"
  tar -xzf "$WORK/dl/semver.tgz" -C "$WORK/semver"
  python3 - "$LESSON/code" "$FIXTURES/available.json" "$WORK/npm-cases.json" "$WORK/npm-mine.json" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
import main
versions = sorted({v for vs in json.load(open(sys.argv[2]))["npm"].values() for v in vs} | {"0.2.3", "0.2.9", "0.3.0", "0.0.3", "0.0.4", "1.2.0", "1.2.9", "1.3.0", "1.9.0", "2.3.9", "2.4.0", "1.2.4-beta.2", "1.2.3-beta.3", "1.0.0-rc.1", "1.3.0-0", "1.0.0-alpha.beta", "1.0.0-beta.11", "1.0.0-beta.2"})
ranges = ["^4.18.2", "4.17.20", "~3.22.4", "^0.4.1", "4.x", "~5.4.5", ">=8.0.0 <9.0.0", "^2.8.8 || ^3.0.0", "^0.2.3", "^0.0.3", "~1.2", "1.x", "1.2.3 - 2.3", ">= 8.0.0 < 9.0.0", ">=1.2.4-beta.1", "*", "^1.2.3-beta.2", "~0.0.1", "<=1.2", ">1.2", "<1.2", "^0.x", "=1.2.0"]
json.dump({"versions": versions, "ranges": ranges}, open(sys.argv[3], "w"))
mine = {
    "sorted": [str(v) for v in sorted((main.parse_semver(v) for v in versions), key=main.SemVer.precedence)],
    "ranges": {r: [v for v in versions if main.npm_satisfies(main.parse_semver(v), main.npm_range(r))] for r in ranges},
}
json.dump(mine, open(sys.argv[4], "w"))
PY
  cat > "$WORK/npm-check.js" <<'EOF'
const fs = require("fs");
const semver = require(process.argv[2]);
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = { sorted: semver.sort([...cases.versions]), ranges: {} };
for (const range of cases.ranges) out.ranges[range] = cases.versions.filter((v) => semver.satisfies(v, range));
fs.writeFileSync(process.argv[4], JSON.stringify(out));
EOF
  node "$WORK/npm-check.js" "$WORK/semver/package" "$WORK/npm-cases.json" "$WORK/npm-theirs.json" > "$WORK/node.txt" 2>&1 || fail "node-semver: $(cat "$WORK/node.txt")"
  python3 - "$WORK/npm-mine.json" "$WORK/npm-theirs.json" <<'PY' || fail "npm range comparison with node-semver"
import json, sys
mine, theirs = (json.load(open(p)) for p in sys.argv[1:3])
assert mine["sorted"] == theirs["sorted"], (mine["sorted"], theirs["sorted"])
problems = [f"{r!r}: minirelease {sorted(set(mine['ranges'][r]))} node-semver {sorted(set(theirs['ranges'][r]))}" for r in mine["ranges"] if set(mine["ranges"][r]) != set(theirs["ranges"][r])]
assert not problems, "\n".join(problems)
print(f"  {len(mine['sorted'])} versions sorted identically; {len(mine['ranges'])} ranges agree with node-semver")
PY
else
  echo "  (node not installed; the node-semver comparison is skipped)"
fi

echo "SMOKE OK: cicd/release-versioning"
