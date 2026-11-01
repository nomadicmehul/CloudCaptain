#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

LESSON_DIR="$PWD"
MINISBOM=(python3 "$LESSON_DIR/code/main.py")
FIX="$LESSON_DIR/code/fixtures"
SYFT_VERSION="1.55.0"
DEB_BASE="python:3.12-slim-trixie"
APK_BASE="python:3.12-alpine3.22"
PREFIX="cc-sbom"
BUILDER="cc-sbom-builder"
IDNA_ADVISORY="GHSA-jjg7-2v4v-x38h"
export SOURCE_DATE_EPOCH=1767225600
export SYFT_CHECK_FOR_APP_UPDATE=false
WORK="$(mktemp -d)"
IMAGES=()
CONTAINERS=()
BUILDER_CREATED=""
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
cleanup() {
  for c in ${CONTAINERS[@]+"${CONTAINERS[@]}"}; do docker rm -f "$c" >/dev/null 2>&1 || true; done
  for i in ${IMAGES[@]+"${IMAGES[@]}"}; do docker rmi -f "$i" >/dev/null 2>&1 || true; done
  if [[ -n "$BUILDER_CREATED" ]]; then docker buildx rm "$BUILDER" >/dev/null 2>&1 || true; fi
  rm -rf "$WORK" 2>/dev/null || sudo rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT
mkdir -p "$WORK/bin" "$WORK/dl" "$WORK/ctx" "$WORK/app"
export PATH="$WORK/bin:$PATH"

sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }
run_expect() {
  local want="$1" out="$2" got=0
  shift 2
  "$@" > "$out" 2> "$out.err" || got=$?
  [[ "$got" == "$want" ]] || fail "exit $got (want $want) from: $* :: $(tail -n 40 "$out" "$out.err")"
}

cat > "$WORK/compare.py" <<'PY'
import json
import re
import sys

KEEP = ("deb", "apk", "pypi")


def norm(kind, name):
    return re.sub(r"[-_.]+", "-", name).lower() if kind == "pypi" else name


def unwrap(doc):
    if isinstance(doc, dict) and str(doc.get("_type", "")).startswith("https://in-toto.io/Statement") and isinstance(doc.get("predicate"), dict):
        return doc["predicate"]
    return doc


def purl_type(purl):
    return purl[4:].split("/", 1)[0] if purl.startswith("pkg:") else ""


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def read_cdx(path):
    found = set()

    def walk(items):
        for item in items or []:
            kind = purl_type(item.get("purl") or "")
            if kind:
                found.add((kind, norm(kind, item["name"]), str(item.get("version", ""))))
            walk(item.get("components"))

    walk(unwrap(load(path)).get("components"))
    return found


def read_spdx(path):
    found = set()
    for item in unwrap(load(path)).get("packages") or []:
        for ref in item.get("externalRefs") or []:
            if ref.get("referenceType") == "purl":
                kind = purl_type(ref.get("referenceLocator", ""))
                if kind:
                    found.add((kind, norm(kind, item["name"]), str(item.get("versionInfo", ""))))
    return found


def read_mini(path):
    return {(p["type"], norm(p["type"], p["name"]), p["version"]) for p in load(path)["packages"]}


def read_dpkg(path):
    found = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3 and parts[2][1:2] not in ("", "n", "c"):
                found.add(("deb", parts[0], parts[1]))
    return found


def read_apk(path):
    found = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            match = re.match(r"^(.+?)-(\d[^-\s]*(?:-r\d+)?)\s", line)
            if match and "[installed]" in line:
                found.add(("apk", match.group(1), match.group(2)))
    return found


def read_pip(path):
    return {("pypi", norm("pypi", p["name"]), p["version"]) for p in load(path)}


READERS = {"cdx": read_cdx, "spdx": read_spdx, "mini": read_mini, "dpkg": read_dpkg, "apk": read_apk, "pip": read_pip}


def comparable(rows, wanted):
    return {r for r in rows if r[0] in wanted and not (r[0] == "apk" and r[1].startswith("."))}


def counts(rows):
    return ", ".join(f"{kind} {sum(1 for r in rows if r[0] == kind)}" for kind in KEEP if any(r[0] == kind for r in rows))


def same(label, kind_a, path_a, kind_b, path_b, *types):
    wanted = set(types) or set(KEEP)
    a, b = comparable(READERS[kind_a](path_a), wanted), comparable(READERS[kind_b](path_b), wanted)
    if not a:
        sys.exit(f"{label}: {kind_a} found no {sorted(wanted)} packages in {path_a}")
    if a != b:
        sys.exit(f"{label}: {kind_a} and {kind_b} disagree\n  only {kind_a}: {sorted(a - b)[:25]}\n  only {kind_b}: {sorted(b - a)[:25]}")
    print(f"  {label}: {kind_a} == {kind_b} ({counts(a)})")


def diff_rows(path, wanted):
    data = load(path)
    rows = set()
    for side in ("added", "removed"):
        for c in data[side]:
            if c["type"] in wanted and not (c["type"] == "apk" and c["name"].startswith(".")):
                rows.add((side, c["type"], norm(c["type"], c["name"]), c["version"], ""))
    for c in data["changed"]:
        if c["type"] in wanted:
            rows.add(("changed", c["type"], norm(c["type"], c["name"]), c["from"], c["to"]))
    return rows


def diffs(label, mine, theirs):
    a, b = diff_rows(mine, set(KEEP)), diff_rows(theirs, set(KEEP))
    if a != b:
        sys.exit(f"{label}: the diffs disagree\n  only in the first: {sorted(a - b)}\n  only in the second: {sorted(b - a)}")
    print(f"  {label}: {len(a)} change(s), identical in both")


def planted(label, path, kind, added_name):
    rows = diff_rows(path, set(KEEP))
    python_rows = {r for r in rows if r[1] == "pypi"}
    want = {("changed", "pypi", "idna", "3.6", "3.7"), ("removed", "pypi", "six", "1.17.0", "")}
    if python_rows != want:
        sys.exit(f"{label}: the Python changes are not exactly the planted ones\n  got: {sorted(python_rows)}\n  want: {sorted(want)}")
    if not any(r[0] == "added" and r[1] == kind and r[2] == added_name for r in rows):
        sys.exit(f"{label}: {kind} {added_name} should be added: {sorted(rows)}")
    directions = {c["name"]: c["direction"] for c in load(path)["changed"]}
    if directions.get("idna") != "upgrade":
        sys.exit(f"{label}: idna should be an upgrade: {directions}")
    extra = sorted(r for r in rows if r[1] == kind and r[2] != added_name)
    print(f"  {label}: idna 3.6 -> 3.7 (upgrade), six removed, {kind} {added_name} added" + (f", plus {extra}" if extra else ""))


def expect_rows(label, path, *rows):
    got = diff_rows(path, {"deb", "apk", "pypi", "npm"})
    want = {tuple(r.split("|")) for r in rows}
    if got != want:
        sys.exit(f"{label}: diff rows differ\n  missing: {sorted(want - got)}\n  extra: {sorted(got - want)}")
    print(f"  {label}: exactly the {len(want)} planted changes")


def findings(label, path, *expected):
    data = load(path)
    got = {(f["advisory"], norm(f["package"]["type"], f["package"]["name"]), f["package"]["version"]) for f in data["findings"]}
    want = {tuple(e.split("|")) for e in expected}
    if got != want:
        sys.exit(f"{label}: findings differ\n  missing: {sorted(want - got)}\n  extra: {sorted(got - want)}\n  notes: {data.get('notes')}")
    print(f"  {label}: {len(got)} finding(s) " + (", ".join(f"{a} {n} {v}" for a, n, v in sorted(got)) if got else "(none)"))


def pairs(label, mine, real):
    def read(path):
        result = {}
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) == 3:
                    result[(parts[0], parts[1])] = parts[2]
        return result

    a, b = read(mine), read(real)
    if not a or set(a) != set(b):
        sys.exit(f"{label}: pair sets differ: {sorted(set(a) ^ set(b))}")
    wrong = sorted((k, a[k], b[k]) for k in a if a[k] != b[k])
    if wrong:
        sys.exit(f"{label}: minisbom and the real tool disagree on {wrong}")
    print(f"  {label}: {len(a)} pairs agree")


def npm(label, mine, theirs, root):
    def read(path):
        return {(item["name"], str(item.get("version", ""))) for item in load(path).get("components") or [] if str(item.get("purl", "")).startswith("pkg:npm/")}

    a, b = read(mine), read(theirs)
    if not a or not a <= b:
        sys.exit(f"{label}: syft is missing npm packages minisbom found: {sorted(a - b)}")
    extra = {name for name, _ in b - a}
    if not extra <= {root}:
        sys.exit(f"{label}: syft found npm packages minisbom did not: {sorted(b - a)}")
    print(f"  {label}: {len(a)} npm packages agree" + (f" (syft also lists the project {root} itself)" if extra else ""))


COMMANDS = {"same": same, "diffs": diffs, "planted": planted, "rows": expect_rows, "findings": findings, "pairs": pairs, "npm": npm}
COMMANDS[sys.argv[1]](*sys.argv[2:])
PY
CMP=(python3 "$WORK/compare.py")

echo "▸ minisbom on the committed fixtures: inventory, diff, match"
run_expect 0 "$WORK/fixture-scan.txt" "${MINISBOM[@]}" scan "$FIX/debian-v1"
grep -q "14 packages: deb 6, pypi 2, npm 6" "$WORK/fixture-scan.txt" || fail "fixture inventory: $(cat "$WORK/fixture-scan.txt")"
run_expect 1 "$WORK/fixture-diff.json" "${MINISBOM[@]}" diff "$FIX/debian-v1" "$FIX/debian-v2" --json
"${CMP[@]}" rows "fixture diff" "$WORK/fixture-diff.json" \
  "added|deb|less|590-2.1~deb12u2|" "removed|deb|tzdata|2024a-0+deb12u1|" "removed|pypi|six|1.16.0|" \
  "changed|deb|libc6|2.36-9+deb12u4|2.36-9+deb12u10" "changed|deb|libssl3|3.0.11-1~deb12u2|3.0.15-1~deb12u1" \
  "changed|deb|login|1:4.13+dfsg1-1|1:4.13+dfsg1-1+deb12u1" "changed|deb|passwd|1:4.13+dfsg1-1|1:4.13+dfsg1-1+deb12u1" \
  "changed|pypi|idna|3.6|3.7" "changed|npm|@babel/traverse|7.23.1|7.23.2" "changed|npm|semver|7.5.1|7.5.4" || fail "fixture diff"
run_expect 1 "$WORK/fixture-v1-match.json" "${MINISBOM[@]}" match "$FIX/debian-v1" --advisories "$FIX/advisories" --json
"${CMP[@]}" findings "debian-v1 fixture" "$WORK/fixture-v1-match.json" \
  "DEBIAN-CVE-2023-29383|login|1:4.13+dfsg1-1" "DEBIAN-CVE-2023-29383|passwd|1:4.13+dfsg1-1" \
  "GHSA-67hx-6x53-jw92|@babel/traverse|7.23.1" "GHSA-c2qf-rxjj-qqgw|semver|7.5.1" "$IDNA_ADVISORY|idna|3.6" || fail "fixture match v1"
run_expect 0 "$WORK/fixture-v2-match.json" "${MINISBOM[@]}" match "$FIX/debian-v2" --advisories "$FIX/advisories" --json
"${CMP[@]}" findings "debian-v2 fixture" "$WORK/fixture-v2-match.json" || fail "fixture match v2"
run_expect 1 "$WORK/fixture-alpine-match.json" "${MINISBOM[@]}" match "$FIX/alpine" --advisories "$FIX/advisories" --json
"${CMP[@]}" findings "alpine fixture" "$WORK/fixture-alpine-match.json" \
  "ALPINE-CVE-2023-42363|busybox|1.36.1-r15" "ALPINE-CVE-2023-42363|busybox-binsh|1.36.1-r15" "ALPINE-CVE-2023-42363|ssl_client|1.36.1-r15" || fail "fixture match alpine"

echo "▸ SOURCE_DATE_EPOCH makes the documents byte-identical across runs"
for n in 1 2; do
  run_expect 0 "$WORK/repro-$n.log.txt" "${MINISBOM[@]}" scan "$FIX/debian-v1" --format spdx -o "$WORK/repro-$n.spdx.json"
  run_expect 0 "$WORK/repro-$n.cdx.txt" "${MINISBOM[@]}" scan "$FIX/debian-v1" --format cyclonedx -o "$WORK/repro-$n.cdx.json"
done
cmp -s "$WORK/repro-1.spdx.json" "$WORK/repro-2.spdx.json" || fail "SPDX output differs between two runs"
cmp -s "$WORK/repro-1.cdx.json" "$WORK/repro-2.cdx.json" || fail "CycloneDX output differs between two runs"

echo "▸ download syft v$SYFT_VERSION and verify its sha256 against the release's checksums.txt"
case "$(uname -s)" in
  Linux) OS=linux ;;
  Darwin) OS=darwin ;;
  *) fail "unsupported OS $(uname -s)" ;;
esac
case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64 ;;
  arm64|aarch64) ARCH=arm64 ;;
  *) fail "unsupported architecture $(uname -m)" ;;
esac
ASSET="syft_${SYFT_VERSION}_${OS}_${ARCH}.tar.gz"
BASE="https://github.com/anchore/syft/releases/download/v${SYFT_VERSION}"
curl -fsSL --retry 3 -o "$WORK/dl/$ASSET" "$BASE/$ASSET" || fail "download $BASE/$ASSET"
curl -fsSL --retry 3 -o "$WORK/dl/checksums.txt" "$BASE/syft_${SYFT_VERSION}_checksums.txt" || fail "download the syft checksums"
WANT="$(awk -v f="$ASSET" '$2 == f {print $1}' "$WORK/dl/checksums.txt")"
GOT="$(sha256_of "$WORK/dl/$ASSET")"
[[ -n "$WANT" && "$WANT" == "$GOT" ]] || fail "syft checksum mismatch: want '$WANT' got '$GOT'"
tar -xzf "$WORK/dl/$ASSET" -C "$WORK/bin" syft
chmod +x "$WORK/bin/syft"
syft version > "$WORK/syft-version.txt" 2>&1 || fail "syft version: $(cat "$WORK/syft-version.txt")"
grep -q "$SYFT_VERSION" "$WORK/syft-version.txt" || fail "unexpected syft version: $(cat "$WORK/syft-version.txt")"
echo "  syft $SYFT_VERSION, sha256 $GOT"

echo "▸ npm lock file: minisbom and syft dir: list the same packages"
cp "$FIX/debian-v1/srv/app/npm-shrinkwrap.json" "$WORK/app/package-lock.json"
cat > "$WORK/app/package.json" <<'EOF'
{"name": "cc-demo-app", "version": "1.0.0", "license": "MIT", "dependencies": {"@babel/traverse": "^7.23.0", "debug": "^4.3.4", "ms": "^2.1.3", "semver": "^7.5.0"}, "devDependencies": {"left-pad": "1.3.0"}}
EOF
run_expect 0 "$WORK/mini-app.txt" "${MINISBOM[@]}" scan "$WORK/app" --format cyclonedx -o "$WORK/mini-app.cdx.json"
SYFT_JAVASCRIPT_INCLUDE_DEV_DEPENDENCIES=true syft scan "dir:$WORK/app" -q -o cyclonedx-json="$WORK/syft-app.cdx.json" 2> "$WORK/syft-app.err" || fail "syft dir scan: $(cat "$WORK/syft-app.err")"
"${CMP[@]}" npm "package-lock.json" "$WORK/mini-app.cdx.json" "$WORK/syft-app.cdx.json" cc-demo-app || fail "npm comparison"

if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
  echo "  (docker not available; image builds, syft image scans and version checks against dpkg and apk skipped)"
  echo "SMOKE OK: cicd/sbom (fixtures and lock file only)"
  exit 0
fi

echo "▸ pull $DEB_BASE and $APK_BASE"
docker pull -q "$DEB_BASE" > "$WORK/pull-deb.txt" 2>&1 || fail "docker pull $DEB_BASE: $(cat "$WORK/pull-deb.txt")"
docker pull -q "$APK_BASE" > "$WORK/pull-apk.txt" 2>&1 || fail "docker pull $APK_BASE: $(cat "$WORK/pull-apk.txt")"

echo "▸ version order: minisbom compare against dpkg, apk, pip's packaging and npm's semver"
python3 - "$FIX/version-pairs.json" "$WORK" "$LESSON_DIR/code" <<'PY'
import json, sys
pairs, work = json.load(open(sys.argv[1])), sys.argv[2]
sys.path.insert(0, sys.argv[3])
import main
for kind in ("deb", "apk", "pypi", "npm"):
    with open(f"{work}/pairs-{kind}.txt", "w") as plain, open(f"{work}/mine-{kind}.txt", "w") as mine:
        for a, b, _ in pairs[kind]:
            plain.write(f"{a} {b}\n")
            mine.write(f"{a} {b} {'<=>'[main.compare_versions(kind, a, b) + 1]}\n")
PY
docker run --rm -i "$DEB_BASE" sh -c 'while read -r a b; do if dpkg --compare-versions "$a" lt "$b"; then r="<"; elif dpkg --compare-versions "$a" eq "$b"; then r="="; else r=">"; fi; echo "$a $b $r"; done' \
  < "$WORK/pairs-deb.txt" > "$WORK/real-deb.txt" 2> "$WORK/real-deb.err" || fail "dpkg --compare-versions: $(cat "$WORK/real-deb.err")"
"${CMP[@]}" pairs "dpkg --compare-versions" "$WORK/mine-deb.txt" "$WORK/real-deb.txt" || fail "dpkg order"
docker run --rm -i "$APK_BASE" sh -c 'while read -r a b; do echo "$a $b $(apk version -t "$a" "$b")"; done' \
  < "$WORK/pairs-apk.txt" > "$WORK/real-apk.txt" 2> "$WORK/real-apk.err" || fail "apk version -t: $(cat "$WORK/real-apk.err")"
"${CMP[@]}" pairs "apk version -t" "$WORK/mine-apk.txt" "$WORK/real-apk.txt" || fail "apk order"
cat > "$WORK/pep440.py" <<'PY'
import sys
from pip._vendor.packaging.version import Version
for line in sys.stdin:
    a, b = line.split()
    x, y = Version(a), Version(b)
    print(a, b, "<" if x < y else ">" if x > y else "=")
PY
docker run --rm -i "$DEB_BASE" python -c "$(cat "$WORK/pep440.py")" < "$WORK/pairs-pypi.txt" > "$WORK/real-pypi.txt" 2> "$WORK/real-pypi.err" || fail "packaging.version: $(cat "$WORK/real-pypi.err")"
"${CMP[@]}" pairs "pip's packaging.version" "$WORK/mine-pypi.txt" "$WORK/real-pypi.txt" || fail "PEP 440 order"
SEMVER_DIR=""
if command -v node >/dev/null && command -v npm >/dev/null; then SEMVER_DIR="$(npm root -g 2>/dev/null)/npm/node_modules/semver"; fi
if [[ -n "$SEMVER_DIR" && -f "$SEMVER_DIR/package.json" ]]; then
  node -e 'const semver = require(process.argv[1]); for (const line of require("fs").readFileSync(0, "utf8").trim().split("\n")) { const [a, b] = line.split(" "); const r = semver.compare(a, b); console.log(`${a} ${b} ${r < 0 ? "<" : r > 0 ? ">" : "="}`); }' "$SEMVER_DIR" \
    < "$WORK/pairs-npm.txt" > "$WORK/real-npm.txt" 2> "$WORK/real-npm.err" || fail "npm semver: $(cat "$WORK/real-npm.err")"
  "${CMP[@]}" pairs "npm's semver" "$WORK/mine-npm.txt" "$WORK/real-npm.txt" || fail "SemVer order"
else
  echo "  (node or npm's bundled semver not installed; SemVer pairs checked by the unit tests only)"
fi

echo "▸ build two Debian and two Alpine images that differ by planted packages"
cat > "$WORK/ctx/deb-v1.Dockerfile" <<EOF
FROM $DEB_BASE
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore idna==3.6 six==1.17.0
EOF
cat > "$WORK/ctx/deb-v2.Dockerfile" <<EOF
FROM $DEB_BASE
RUN apt-get update && apt-get install -y --no-install-recommends less && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore idna==3.7
EOF
cat > "$WORK/ctx/apk-v1.Dockerfile" <<EOF
FROM $APK_BASE
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore idna==3.6 six==1.17.0
EOF
cat > "$WORK/ctx/apk-v2.Dockerfile" <<EOF
FROM $APK_BASE
RUN apk add --no-cache tini
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore idna==3.7
EOF
for name in deb-v1 deb-v2 apk-v1 apk-v2; do
  IMAGES+=("$PREFIX:$name")
  docker build -q -t "$PREFIX:$name" -f "$WORK/ctx/$name.Dockerfile" "$WORK/ctx" > "$WORK/build-$name.txt" 2>&1 || fail "docker build $name: $(tail -n 30 "$WORK/build-$name.txt")"
done
echo "  built ${IMAGES[*]}"

echo "▸ export each filesystem and scan it with minisbom (deb-v1 from a file, apk-v1 piped on stdin)"
for name in deb-v1 deb-v2 apk-v1 apk-v2; do
  CONTAINERS+=("$PREFIX-$name")
  docker create --name "$PREFIX-$name" "$PREFIX:$name" > /dev/null || fail "docker create $name"
  if [[ "$name" == "apk-v1" ]]; then
    if ! docker export "$PREFIX-$name" | "${MINISBOM[@]}" scan - --name "$name" --format cyclonedx -o "$WORK/mini-$name.cdx.json" 2> "$WORK/mini-$name.err"; then
      fail "docker export $name | minisbom scan -: $(cat "$WORK/mini-$name.err")"
    fi
  else
    docker export "$PREFIX-$name" -o "$WORK/$name.tar" || fail "docker export $name"
    run_expect 0 "$WORK/mini-$name.txt" "${MINISBOM[@]}" scan "$WORK/$name.tar" --name "$name" --format cyclonedx -o "$WORK/mini-$name.cdx.json"
  fi
  docker rm "$PREFIX-$name" > /dev/null
done

echo "▸ syft on the same images: CycloneDX and SPDX"
for name in deb-v1 deb-v2 apk-v1 apk-v2; do
  syft scan "docker:$PREFIX:$name" -q -o cyclonedx-json="$WORK/syft-$name.cdx.json" -o spdx-json="$WORK/syft-$name.spdx.json" 2> "$WORK/syft-$name.err" \
    || fail "syft scan $name: $(cat "$WORK/syft-$name.err")"
done

echo "▸ the package managers themselves: dpkg-query, apk list, pip list inside each image"
for name in deb-v1 deb-v2; do
  docker run --rm "$PREFIX:$name" dpkg-query -W -f '${Package}\t${Version}\t${db:Status-Abbrev}\n' > "$WORK/dpkg-$name.txt" 2> "$WORK/dpkg-$name.err" || fail "dpkg-query in $name: $(cat "$WORK/dpkg-$name.err")"
done
for name in apk-v1 apk-v2; do
  docker run --rm "$PREFIX:$name" apk list --installed > "$WORK/apk-$name.txt" 2> "$WORK/apk-$name.err" || fail "apk list in $name: $(cat "$WORK/apk-$name.err")"
done
for name in deb-v1 deb-v2 apk-v1 apk-v2; do
  docker run --rm "$PREFIX:$name" python -m pip list --format json --disable-pip-version-check > "$WORK/pip-$name.json" 2> "$WORK/pip-$name.err" || fail "pip list in $name: $(cat "$WORK/pip-$name.err")"
done

echo "▸ minisbom == syft == the package manager, per image (name and version, parsed; apk virtual packages excluded)"
for name in deb-v1 deb-v2 apk-v1 apk-v2; do
  "${CMP[@]}" same "$name" cdx "$WORK/mini-$name.cdx.json" cdx "$WORK/syft-$name.cdx.json" || fail "$name: minisbom against syft CycloneDX"
  "${CMP[@]}" same "$name" cdx "$WORK/mini-$name.cdx.json" spdx "$WORK/syft-$name.spdx.json" || fail "$name: minisbom against syft SPDX"
  "${CMP[@]}" same "$name" cdx "$WORK/mini-$name.cdx.json" pip "$WORK/pip-$name.json" pypi || fail "$name: minisbom against pip list"
done
for name in deb-v1 deb-v2; do
  "${CMP[@]}" same "$name" cdx "$WORK/mini-$name.cdx.json" dpkg "$WORK/dpkg-$name.txt" deb || fail "$name: minisbom against dpkg-query"
done
for name in apk-v1 apk-v2; do
  "${CMP[@]}" same "$name" cdx "$WORK/mini-$name.cdx.json" apk "$WORK/apk-$name.txt" apk || fail "$name: minisbom against apk list"
done

echo "▸ diff: minisbom's SBOMs, syft's SBOMs and a cross-format pair give the same changes, and they are the planted ones"
for base in deb apk; do
  run_expect 1 "$WORK/diff-mini-$base.json" "${MINISBOM[@]}" diff "$WORK/mini-$base-v1.cdx.json" "$WORK/mini-$base-v2.cdx.json" --json
  run_expect 1 "$WORK/diff-syft-$base.json" "${MINISBOM[@]}" diff "$WORK/syft-$base-v1.cdx.json" "$WORK/syft-$base-v2.cdx.json" --json
  run_expect 1 "$WORK/diff-cross-$base.json" "${MINISBOM[@]}" diff "$WORK/mini-$base-v1.cdx.json" "$WORK/syft-$base-v2.spdx.json" --json
  "${CMP[@]}" diffs "$base minisbom vs syft" "$WORK/diff-mini-$base.json" "$WORK/diff-syft-$base.json" || fail "$base diffs"
  "${CMP[@]}" diffs "$base CycloneDX vs SPDX" "$WORK/diff-mini-$base.json" "$WORK/diff-cross-$base.json" || fail "$base cross-format diff"
done
"${CMP[@]}" planted "deb" "$WORK/diff-mini-deb.json" deb less || fail "planted Debian changes"
"${CMP[@]}" planted "apk" "$WORK/diff-mini-apk.json" apk tini || fail "planted Alpine changes"

echo "▸ match: the idna advisory hits 3.6 in v1 and nothing in v2, from minisbom's and from syft's SBOMs"
for base in deb apk; do
  for tool in mini syft; do
    run_expect 1 "$WORK/match-$tool-$base-v1.json" "${MINISBOM[@]}" match "$WORK/$tool-$base-v1.cdx.json" --advisories "$FIX/advisories" --json
    "${CMP[@]}" findings "$base-v1 ($tool)" "$WORK/match-$tool-$base-v1.json" "$IDNA_ADVISORY|idna|3.6" || fail "$base-v1 $tool match"
    run_expect 0 "$WORK/match-$tool-$base-v2.json" "${MINISBOM[@]}" match "$WORK/$tool-$base-v2.cdx.json" --advisories "$FIX/advisories" --json
    "${CMP[@]}" findings "$base-v2 ($tool)" "$WORK/match-$tool-$base-v2.json" || fail "$base-v2 $tool match"
  done
done

echo "▸ syft convert reads minisbom's CycloneDX and writes SPDX with the same packages"
syft convert "$WORK/mini-deb-v1.cdx.json" -o spdx-json="$WORK/converted-deb-v1.spdx.json" 2> "$WORK/convert.err" || fail "syft convert: $(cat "$WORK/convert.err")"
"${CMP[@]}" same "syft convert" cdx "$WORK/mini-deb-v1.cdx.json" spdx "$WORK/converted-deb-v1.spdx.json" || fail "syft convert lost packages"

echo "▸ a directory scan of the extracted filesystem equals the tar scan"
mkdir -p "$WORK/rootfs-deb-v2"
tar -xf "$WORK/deb-v2.tar" -C "$WORK/rootfs-deb-v2" --exclude='dev/*' 2> "$WORK/untar.err" || true
run_expect 0 "$WORK/mini-dir.txt" "${MINISBOM[@]}" scan "$WORK/rootfs-deb-v2" --format cyclonedx -o "$WORK/mini-dir.cdx.json"
"${CMP[@]}" same "directory vs tar" cdx "$WORK/mini-dir.cdx.json" cdx "$WORK/mini-deb-v2.cdx.json" || fail "directory scan: $(tail -n 5 "$WORK/untar.err")"

if [[ "${CC_SBOM_SKIP_BUILDX:-}" == "1" ]]; then
  echo "  (CC_SBOM_SKIP_BUILDX=1; the buildx SBOM attestation check is skipped)"
elif ! docker buildx version > /dev/null 2>&1; then
  echo "  (docker buildx not installed; the buildx SBOM attestation check is skipped)"
else
  echo "▸ docker buildx build --sbom=true: the build's own SBOM equals minisbom's scan of the same output"
  BUILDER_CREATED=1
  docker buildx create --name "$BUILDER" --driver docker-container > "$WORK/buildx-create.txt" 2>&1 || fail "docker buildx create: $(cat "$WORK/buildx-create.txt")"
  docker buildx build --builder "$BUILDER" --sbom=true --output "type=local,dest=$WORK/buildx-out" -f "$WORK/ctx/apk-v2.Dockerfile" "$WORK/ctx" > "$WORK/buildx.txt" 2>&1 \
    || fail "docker buildx build --sbom=true: $(tail -n 30 "$WORK/buildx.txt")"
  [[ -f "$WORK/buildx-out/sbom.spdx.json" ]] || fail "no sbom.spdx.json in the local export: $(ls "$WORK/buildx-out")"
  run_expect 0 "$WORK/mini-buildx.txt" "${MINISBOM[@]}" scan "$WORK/buildx-out" --format cyclonedx -o "$WORK/mini-buildx.cdx.json"
  "${CMP[@]}" same "buildx attestation" cdx "$WORK/mini-buildx.cdx.json" spdx "$WORK/buildx-out/sbom.spdx.json" apk pypi || fail "buildx SBOM against minisbom"
  python3 - "$WORK/buildx-out/sbom.spdx.json" "$LESSON_DIR/code" <<'PY' || fail "minisbom should read the in-toto statement buildx wrote"
import sys
sys.path.insert(0, sys.argv[2])
import main
sbom = main.load_sbom(sys.argv[1])
names = {(c.type, c.name, c.version) for c in sbom.components}
assert ("pypi", "idna", "3.7") in names and any(c.type == "apk" and c.name == "tini" for c in sbom.components), sorted(names)[:20]
print(f"  minisbom unwrapped the in-toto statement: {len(sbom.components)} components, idna 3.7 and tini present")
PY
fi

echo "SMOKE OK: cicd/sbom"
