#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

LESSON_DIR="$PWD"
MINIVULN=(python3 "$LESSON_DIR/code/main.py")
FIX="$LESSON_DIR/code/fixtures"
TRIVY_VERSION="0.76.1"
GRYPE_VERSION="0.121.0"
VEXCTL_VERSION="0.4.4"
CVSS_DATA_COMMIT="2f149099257ae06b98cef252efc440bddafe61e5"
CVSS_BASE4_SHA256="4130bbd42649ab744bceb67f5da1b4e839398ea958c21a0b182b9693045d6bec"
CVSS_SIMPLE31_SHA256="6fbab0d5f4ac93d313aed1d088f2267f9856c33fa927d514602a59544d96baaa"
OLD_REF="debian@sha256:24c92a69df28b21676d721fe18c0bf64138bfc69b486746ad935b49cc31b0b91"
NEW_REF="debian@sha256:60eac759739651111db372c07be67863818726f754804b8707c90979bda511df"
OLD_TAG="cc-vuln/debian:12.1-slim"
NEW_TAG="cc-vuln/debian:12.14-slim"
KEV_CVE="CVE-2023-4911"
WORK="$(mktemp -d)"
IMAGES=()
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
cleanup() {
  for i in ${IMAGES[@]+"${IMAGES[@]}"}; do docker rmi "$i" >/dev/null 2>&1 || true; done
  rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT
mkdir -p "$WORK/bin" "$WORK/dl" "$WORK/cache/trivy" "$WORK/cache/grype" "$WORK/data"
export PATH="$WORK/bin:$PATH"
export TRIVY_CACHE_DIR="$WORK/cache/trivy" TRIVY_NO_PROGRESS=true TRIVY_SKIP_VERSION_CHECK=true TRIVY_DISABLE_TELEMETRY=true
export GRYPE_DB_CACHE_DIR="$WORK/cache/grype" GRYPE_CHECK_FOR_APP_UPDATE=false

sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }
run_expect() {
  local want="$1" out="$2" got=0
  shift 2
  "$@" > "$out" 2> "$out.err" || got=$?
  [[ "$got" == "$want" ]] || fail "exit $got (want $want) from: $* :: $(tail -n 30 "$out" "$out.err")"
}
run_any() {
  local allowed="$1" out="$2" got=0
  shift 2
  "$@" > "$out" 2> "$out.err" || got=$?
  [[ " $allowed " == *" $got "* ]] || fail "exit $got (want one of $allowed) from: $* :: $(tail -n 30 "$out" "$out.err")"
}
fetch_verified() {
  local url="$1" out="$2" sums="$3" name="$4" want got
  curl -fsSL --retry 3 -o "$out" "$url" || fail "download $url"
  want="$(awk -v f="$name" '$2 == f || $2 == "*"f {print $1}' "$sums")"
  got="$(sha256_of "$out")"
  [[ -n "$want" && "$want" == "$got" ]] || fail "checksum of $name: want '$want' got '$got'"
  echo "  $name sha256 $got (from the release checksum file)"
}

cat > "$WORK/check.py" <<'PY'
import json
import sys


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def canonical(primary, others):
    for candidate in [primary] + sorted(set(others) - {primary}):
        if candidate.startswith("CVE-"):
            return candidate
    return primary


def trivy_keys(path):
    keys = set()
    for result in load(path).get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            keys.add((v["PkgName"], v.get("InstalledVersion", ""), canonical(v["VulnerabilityID"], [])))
    return keys


def grype_keys(path, field="matches"):
    keys = set()
    for m in load(path).get(field) or []:
        ids = [m["vulnerability"]["id"]] + [r["id"] for r in m.get("relatedVulnerabilities") or []]
        keys.add((m["artifact"]["name"], m["artifact"].get("version", ""), canonical(ids[0], ids[1:])))
    return keys


def dedupe(trivy, grype, ingest):
    t, g = trivy_keys(trivy), grype_keys(grype)
    merged = load(ingest)["findings"]
    mine = {(f["package"], f["version"], f["vuln"]) for f in merged}
    both = {(f["package"], f["version"], f["vuln"]) for f in merged if len(f["tools"]) == 2}
    assert len(mine) == len(merged), "minivuln returned the same key twice"
    assert mine == t | g, f"merged keys differ from the union: only minivuln {sorted(mine - (t | g))[:5]}, only reports {sorted((t | g) - mine)[:5]}"
    assert both == t & g, f"findings from both tools differ from the overlap: {sorted(both ^ (t & g))[:5]}"
    assert t & g, "the two reports share no finding; the image or the databases are not what this smoke expects"
    print(f"  trivy {len(t)} + grype {len(g)} keys -> {len(mine)} findings; the {len(t & g)} in both are counted once")


def fixtures(ingest, ranked, vexed, diffed):
    merged = load(ingest)["findings"]
    both = sum(1 for f in merged if f["tools"] == ["grype", "trivy"])
    assert (len(merged), both) == (13, 11), f"fixture merge: {len(merged)} findings, {both} from both tools"
    order = [(f["vuln"], f["package"], f["priority"]) for f in load(ranked)["findings"]][:5]
    assert order[0] == ("CVE-2023-4911", "libc-bin", "P1") and order[2][2] == "P2" and order[4] == ("CVE-2023-45853", "zlib1g", "P3"), f"fixture order: {order}"
    suppressed = sorted((f["vuln"], f["package"]) for f in load(vexed)["suppressed"])
    assert suppressed == [("CVE-2023-45853", "zlib1g"), ("CVE-2024-3651", "idna")], f"fixture VEX: {suppressed}"
    d = load(diffed)
    counts = (len(d["fixed"]), len(d["new_findings"]), len(d["still_open"]))
    assert counts == (9, 1, 3), f"fixture diff fixed/new/still: {counts}"
    print(f"  fixtures: 13 merged (11 from both), top {order[0][0]} P1, 2 suppressed by VEX, diff fixed 9 / new 1 / still open 3")


def cvssfile(expected, computed):
    want = []
    with open(expected, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                vector, _, rest = line.strip().partition(" - ")
                want.append((vector, float(rest.strip("(),").split(",")[0])))
    got = load(computed)
    assert len(want) == len(got), f"{expected}: {len(want)} vectors, minivuln scored {len(got)}"
    bad = [(w[0], w[1], g["score"]) for w, g in zip(want, got) if w[0] != g["vector"] or w[1] != g["score"]]
    assert not bad, f"{len(bad)} of {len(want)} scores differ, first: {bad[:5]}"
    print(f"  {expected.rsplit('/', 1)[-1]}: all {len(want)} published scores reproduced")


def claims(trivy, grype, out):
    rows = []
    for result in load(trivy).get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            for source, data in (v.get("CVSS") or {}).items():
                for vk, sk in (("V3Vector", "V3Score"), ("V40Vector", "V40Score")):
                    if data.get(vk) and data.get(sk) is not None:
                        rows.append({"tool": "trivy", "source": source.lower(), "vector": data[vk], "score": float(data[sk])})
    for m in load(grype).get("matches") or []:
        for meta in [m["vulnerability"]] + list(m.get("relatedVulnerabilities") or []):
            for c in meta.get("cvss") or []:
                vector = c.get("vector") or ""
                score = (c.get("metrics") or {}).get("baseScore")
                if (vector.startswith("CVSS:3") or vector.startswith("CVSS:4")) and score is not None:
                    rows.append({"tool": "grype", "source": str(c.get("source") or "").lower(), "vector": vector, "score": float(score)})
    with open(out + ".json", "w", encoding="utf-8") as handle:
        json.dump(rows, handle)
    with open(out + ".txt", "w", encoding="utf-8") as handle:
        handle.write("\n".join(sorted({r["vector"] for r in rows})) + "\n")
    print(f"  {len(rows)} CVSS claims in the two reports, {len({r['vector'] for r in rows})} distinct vectors")


def scores(claims_json, computed):
    scored = load(computed)
    mine = {s["vector"]: s["score"] for s in scored if s["score"] is not None}
    unscored = sorted(s["vector"] for s in scored if s["score"] is None)
    if unscored:
        print(f"  {len(unscored)} vector(s) in the reports are not valid CVSS v3/v4 and were not compared: {unscored[:3]}")
    rows = [r for r in load(claims_json) if r["vector"] in mine]
    nvd31 = [r for r in rows if r["vector"].startswith("CVSS:3.1/") and "nvd" in r["source"]]
    other31 = [r for r in rows if r["vector"].startswith("CVSS:3.1/") and "nvd" not in r["source"]]
    v30 = [r for r in rows if r["vector"].startswith("CVSS:3.0/")]
    v40 = [r for r in rows if r["vector"].startswith("CVSS:4.0/")]
    wrong = [r for r in nvd31 if mine[r["vector"]] != r["score"]]
    assert len(nvd31) >= 20, f"only {len(nvd31)} NVD CVSS v3.1 claims to compare"
    assert not wrong, f"{len(wrong)} NVD v3.1 scores differ from minivuln: {[(r['vector'], r['score'], mine[r['vector']]) for r in wrong[:5]]}"
    print(f"  NVD CVSS v3.1: {len(nvd31)} claims, all equal to minivuln's scores")
    for label, group, floor in (("other v3.1", other31, 0.98), ("v3.0", v30, 0.98), ("v4.0", v40, 0.9)):
        if not group:
            print(f"  {label}: no claims in these reports")
            continue
        agree = sum(1 for r in group if mine[r["vector"]] == r["score"])
        off = [(r["tool"], r["source"], r["vector"], r["score"], mine[r["vector"]]) for r in group if mine[r["vector"]] != r["score"]]
        print(f"  {label}: {agree} of {len(group)} claims equal minivuln's score" + (f"; differing: {off[:3]}" if off else ""))
        assert agree >= floor * len(group), f"{label}: only {agree} of {len(group)} agree: {off[:5]}"


def purls(trivy, cve):
    found = set()
    for result in load(trivy).get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            if v["VulnerabilityID"] == cve:
                found.add(((v.get("PkgIdentifier") or {}).get("PURL") or "").split("?", 1)[0])
    found.discard("")
    assert found, f"trivy reports no package with {cve}; the old image should still have libc6 2.36-9+deb12u1"
    print("\n".join(sorted(found)))


def vex(trivy_plain, trivy_vex, grype_plain, grype_vex, ranked, cve):
    def pkgs(keys):
        return {k[0] for k in keys if k[2] == cve}

    pt, pt_after = pkgs(trivy_keys(trivy_plain)), pkgs(trivy_keys(trivy_vex))
    pg, pg_after = pkgs(grype_keys(grype_plain)), pkgs(grype_keys(grype_vex))
    ignored = set()
    for m in load(grype_vex).get("ignoredMatches") or []:
        if canonical(m["vulnerability"]["id"], [r["id"] for r in m.get("relatedVulnerabilities") or []]) == cve:
            if any(rule.get("namespace") == "vex" for rule in m.get("appliedIgnoreRules") or []):
                ignored.add(m["artifact"]["name"])
    modified = set()
    for result in load(trivy_vex).get("Results") or []:
        for item in result.get("ExperimentalModifiedFindings") or []:
            finding = item.get("Finding") or {}
            if finding.get("VulnerabilityID") == cve and item.get("Status") == "not_affected":
                modified.add(finding.get("PkgName"))
    doc = load(ranked)
    mine = {f["package"] for f in doc["suppressed"] if f["vuln"] == cve}
    still = [f["package"] for f in doc["findings"] if f["vuln"] == cve]
    assert pt and pg, f"{cve} must be in both plain reports: trivy {sorted(pt)}, grype {sorted(pg)}"
    assert not pt_after, f"trivy --vex still reports {cve} in {sorted(pt_after)}"
    assert not pg_after, f"grype --vex still reports {cve} in {sorted(pg_after)}"
    assert ignored == pg, f"grype ignoredMatches for {cve}: {sorted(ignored)}, expected {sorted(pg)}"
    assert not modified or modified == pt, f"trivy ExperimentalModifiedFindings for {cve}: {sorted(modified)}, expected {sorted(pt)}"
    assert mine == pt | pg and not still, f"minivuln suppressed {sorted(mine)} and kept {still}; trivy {sorted(pt)}, grype {sorted(pg)}"
    print(f"  {cve} suppressed in {', '.join(sorted(mine))} by trivy --vex, grype --vex and minivuln alike" + ("" if modified else " (trivy listed no ExperimentalModifiedFindings)"))
    reasons = [r for f in doc["suppressed"] if f["vuln"] == cve for r in f["reasons"]]
    assert any("second person" in r for r in reasons), f"minivuln should flag a suppressed KEV entry for review: {reasons}"


def top(ranked, kev, cve):
    doc = load(ranked)
    listed = {v["cveID"] for v in load(kev)["vulnerabilities"]}
    rows = doc["findings"]
    assert rows, "minivuln ranked nothing"
    head = [(f["priority"], f["vuln"], f["package"], f["threat"], (f["score"] or {}).get("value")) for f in rows[:5]]
    for row in head:
        print("  ", *row)
    assert rows[0]["vuln"] == cve and rows[0]["priority"] == "P1", f"first row should be the KEV entry {cve}: {head}"
    flagged = {f["vuln"] for f in rows if f["kev"]}
    assert flagged <= listed and cve in flagged, f"KEV flags {sorted(flagged)} must come from the catalogue {sorted(listed)}"
    assert all(f["priority"] == "P1" for f in rows if f["kev"]), "every KEV-listed finding must be P1"
    print(f"  KEV-listed findings ranked first: {sorted(flagged)}; {doc['summary']}")


def diff(old, new, diffed, cve):
    o = {(k[0], k[2]) for k in trivy_keys(old)}
    n = {(k[0], k[2]) for k in trivy_keys(new)}
    d = load(diffed)
    fixed = {(f["package"], f["vuln"]) for f in d["fixed"]}
    still = {(f["package"], f["vuln"]) for f in d["still_open"]}
    added = {(f["package"], f["vuln"]) for f in d["new_findings"]}
    assert fixed == o - n, f"fixed differs from old minus new: only minivuln {sorted(fixed - (o - n))[:5]}, only raw {sorted((o - n) - fixed)[:5]}"
    assert still == o & n, f"still open differs from old and new: {sorted(still ^ (o & n))[:5]}"
    assert added == n - o, f"new differs from new minus old: {sorted(added ^ (n - o))[:5]}"
    assert ("libc6", cve) in fixed, f"{cve} in libc6 should be fixed by the newer base image"
    assert not any(v == cve for _, v in still | added), f"{cve} must be gone from the new image"
    print(f"  diff: {len(fixed)} fixed (including {cve} in libc6), {len(added)} new, {len(still)} still open; each set equals the raw report arithmetic")


def live(ranked, grype, cve):
    doc = load(ranked)
    rows = doc["findings"]
    mine = {f["vuln"] for f in rows if f["kev"]}
    theirs = set()
    for m in load(grype).get("matches") or []:
        for k in m["vulnerability"].get("knownExploited") or []:
            theirs.add(k.get("cve"))
    assert cve in mine, f"{cve} should be in the live KEV catalogue: {sorted(mine)[:10]}"
    print(f"  live feeds: {len(mine)} KEV-listed CVEs in this image per minivuln, {len(theirs)} per grype's knownExploited")
    if theirs - mine or mine - theirs:
        print(f"  (feeds differ in age: only minivuln {sorted(mine - theirs)[:5]}, only grype {sorted(theirs - mine)[:5]})")
    for f in rows[:5]:
        print("  ", f["priority"], f["vuln"], f["package"], "threat", f["threat"])


COMMANDS = {"dedupe": dedupe, "fixtures": fixtures, "cvssfile": cvssfile, "claims": claims, "scores": scores, "purls": purls, "vex": vex, "top": top, "diff": diff, "live": live}
COMMANDS[sys.argv[1]](*sys.argv[2:])
PY
CHECK=(python3 "$WORK/check.py")
F="$FIX"
FEEDS=(--kev "$F/kev-2026-10-30.json" --epss "$F/epss-2026-11-01.csv")

echo "▸ minivuln on the committed fixtures: ingest, rank, VEX, diff"
run_expect 0 "$WORK/fx-ingest.json" "${MINIVULN[@]}" ingest "$F/trivy-api-1.4.0.json" "$F/grype-api-1.4.0.json" --json
run_expect 0 "$WORK/fx-rank.json" "${MINIVULN[@]}" rank "$F/trivy-api-1.4.0.json" "$F/grype-api-1.4.0.json" "${FEEDS[@]}" --exposure internet --now 2026-11-02 --json
run_expect 0 "$WORK/fx-vex.json" "${MINIVULN[@]}" rank "$F/trivy-api-1.4.0.json" "$F/grype-api-1.4.0.json" "${FEEDS[@]}" --vex "$F/vex-api.openvex.json" --exposure internet --now 2026-11-02 --json
run_expect 1 "$WORK/fx-diff.json" "${MINIVULN[@]}" diff --old "$F/trivy-api-1.4.0.json" --new "$F/trivy-api-1.5.0.json" "${FEEDS[@]}" --now 2026-11-02 --json
run_expect 1 "$WORK/fx-gate.txt" "${MINIVULN[@]}" rank "$F/trivy-api-1.4.0.json" "${FEEDS[@]}" --now 2026-11-02 --fail-on P1
run_expect 0 "$WORK/fx-vexlint.txt" "${MINIVULN[@]}" vex "$F/vex-api.openvex.json"
"${CHECK[@]}" fixtures "$WORK/fx-ingest.json" "$WORK/fx-rank.json" "$WORK/fx-vex.json" "$WORK/fx-diff.json" || fail "fixture checks"
echo '{"@context": "https://openvex.dev/ns/v0.2.0", "statements": [{"vulnerability": {"name": "CVE-2023-4911"}, "products": [{"@id": "pkg:deb/debian/libc6"}], "status": "not_affected"}]}' > "$WORK/bad-vex.json"
run_expect 2 "$WORK/fx-badvex.txt" "${MINIVULN[@]}" rank "$F/trivy-api-1.4.0.json" --vex "$WORK/bad-vex.json" --now 2026-11-02
grep -q "not_affected needs a justification" "$WORK/fx-badvex.txt.err" || fail "an invalid VEX statement should be refused: $(cat "$WORK/fx-badvex.txt.err")"

echo "▸ CVSS: every published base vector in Red Hat's cvss test data (commit ${CVSS_DATA_COMMIT:0:12}), sha256 pinned from our own download"
BASE="https://raw.githubusercontent.com/RedHatProductSecurity/cvss/$CVSS_DATA_COMMIT/tests"
curl -fsSL --retry 3 -o "$WORK/data/vectors_simple31" "$BASE/vectors_simple31" || fail "download vectors_simple31"
curl -fsSL --retry 3 -o "$WORK/data/vectors_base4" "$BASE/vectors_base4" || fail "download vectors_base4"
[[ "$(sha256_of "$WORK/data/vectors_simple31")" == "$CVSS_SIMPLE31_SHA256" ]] || fail "vectors_simple31 sha256 is $(sha256_of "$WORK/data/vectors_simple31")"
[[ "$(sha256_of "$WORK/data/vectors_base4")" == "$CVSS_BASE4_SHA256" ]] || fail "vectors_base4 sha256 is $(sha256_of "$WORK/data/vectors_base4")"
run_expect 0 "$WORK/simple31.json" "${MINIVULN[@]}" cvss --file "$WORK/data/vectors_simple31" --json
run_expect 0 "$WORK/base4.json" "${MINIVULN[@]}" cvss --file "$WORK/data/vectors_base4" --json
"${CHECK[@]}" cvssfile "$WORK/data/vectors_simple31" "$WORK/simple31.json" || fail "CVSS v3.1 base vectors"
"${CHECK[@]}" cvssfile "$WORK/data/vectors_base4" "$WORK/base4.json" || fail "CVSS v4.0 base vectors"

if [[ "$(uname -s)" != "Linux" ]] || ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
  echo "  (not Linux or docker not available; Trivy, Grype and vexctl checks skipped)"
  echo "SMOKE OK: security/vuln-prioritization (minivuln only)"
  exit 0
fi

case "$(uname -m)" in
  x86_64|amd64) T_ARCH="Linux-64bit"; G_ARCH="linux_amd64"; V_ARCH="linux-amd64" ;;
  aarch64|arm64) T_ARCH="Linux-ARM64"; G_ARCH="linux_arm64"; V_ARCH="linux-arm64" ;;
  *) fail "unsupported architecture $(uname -m)" ;;
esac

echo "▸ download trivy v$TRIVY_VERSION, grype v$GRYPE_VERSION and vexctl v$VEXCTL_VERSION, verified against each release's checksum file"
TB="https://github.com/aquasecurity/trivy/releases/download/v$TRIVY_VERSION"
curl -fsSL --retry 3 -o "$WORK/dl/trivy-sums.txt" "$TB/trivy_${TRIVY_VERSION}_checksums.txt" || fail "download trivy checksums"
fetch_verified "$TB/trivy_${TRIVY_VERSION}_${T_ARCH}.tar.gz" "$WORK/dl/trivy.tgz" "$WORK/dl/trivy-sums.txt" "trivy_${TRIVY_VERSION}_${T_ARCH}.tar.gz"
GB="https://github.com/anchore/grype/releases/download/v$GRYPE_VERSION"
curl -fsSL --retry 3 -o "$WORK/dl/grype-sums.txt" "$GB/grype_${GRYPE_VERSION}_checksums.txt" || fail "download grype checksums"
fetch_verified "$GB/grype_${GRYPE_VERSION}_${G_ARCH}.tar.gz" "$WORK/dl/grype.tgz" "$WORK/dl/grype-sums.txt" "grype_${GRYPE_VERSION}_${G_ARCH}.tar.gz"
VB="https://github.com/openvex/vexctl/releases/download/v$VEXCTL_VERSION"
curl -fsSL --retry 3 -o "$WORK/dl/vexctl-sums.txt" "$VB/vexctl_checksums.txt" || fail "download vexctl checksums"
fetch_verified "$VB/vexctl-${V_ARCH}" "$WORK/dl/vexctl" "$WORK/dl/vexctl-sums.txt" "vexctl-${V_ARCH}"
mkdir -p "$WORK/x-trivy" "$WORK/x-grype"
tar -xzf "$WORK/dl/trivy.tgz" -C "$WORK/x-trivy" || fail "unpack trivy"
tar -xzf "$WORK/dl/grype.tgz" -C "$WORK/x-grype" || fail "unpack grype"
find "$WORK/x-trivy" -type f -name trivy -exec install -m 0755 {} "$WORK/bin/trivy" \;
find "$WORK/x-grype" -type f -name grype -exec install -m 0755 {} "$WORK/bin/grype" \;
install -m 0755 "$WORK/dl/vexctl" "$WORK/bin/vexctl"
trivy --version > "$WORK/trivy-version.txt" 2>&1 || fail "trivy --version: $(cat "$WORK/trivy-version.txt")"
grep -q "$TRIVY_VERSION" "$WORK/trivy-version.txt" || fail "unexpected trivy version: $(cat "$WORK/trivy-version.txt")"
grype version > "$WORK/grype-version.txt" 2>&1 || fail "grype version: $(cat "$WORK/grype-version.txt")"
grep -q "$GRYPE_VERSION" "$WORK/grype-version.txt" || fail "unexpected grype version: $(cat "$WORK/grype-version.txt")"
vexctl version > "$WORK/vexctl-version.txt" 2>&1 || true
grep -q "$VEXCTL_VERSION" "$WORK/vexctl-version.txt" || echo "  (vexctl version printed: $(head -n 3 "$WORK/vexctl-version.txt" | tr '\n' ' '))"

echo "▸ pull the old and the new Debian image by digest and tag them locally"
for pair in "$OLD_REF $OLD_TAG" "$NEW_REF $NEW_TAG"; do
  set -- $pair
  docker pull -q "$1" > "$WORK/pull.txt" 2>&1 || fail "docker pull $1: $(cat "$WORK/pull.txt")"
  docker tag "$1" "$2" || fail "docker tag $1 $2"
  IMAGES+=("$2" "$1")
done
docker run --rm "$OLD_TAG" dpkg-query -W -f '${Version}' libc6 > "$WORK/libc6-old.txt" || fail "dpkg-query in the old image"
docker run --rm "$NEW_TAG" dpkg-query -W -f '${Version}' libc6 > "$WORK/libc6-new.txt" || fail "dpkg-query in the new image"
[[ "$(cat "$WORK/libc6-old.txt")" == "2.36-9+deb12u1" ]] || fail "old image libc6 is $(cat "$WORK/libc6-old.txt"), expected 2.36-9+deb12u1"
echo "  libc6 $(cat "$WORK/libc6-old.txt") in $OLD_TAG, $(cat "$WORK/libc6-new.txt") in $NEW_TAG"

echo "▸ scan: trivy (old and new) and grype (old), JSON reports"
run_expect 0 "$WORK/trivy-old.log.txt" trivy image --quiet --scanners vuln --format json --output "$WORK/trivy-old.json" "$OLD_TAG"
run_expect 0 "$WORK/trivy-new.log.txt" trivy image --quiet --scanners vuln --format json --output "$WORK/trivy-new.json" "$NEW_TAG"
run_expect 0 "$WORK/grype-old.log.txt" grype "docker:$OLD_TAG" -q -o json --file "$WORK/grype-old.json"
run_expect 0 "$WORK/ingest.txt" "${MINIVULN[@]}" ingest "$WORK/trivy-old.json" "$WORK/grype-old.json"
head -n 3 "$WORK/ingest.txt"

echo "▸ deduplication: the merged list is the union of both reports' (package, version, advisory) keys"
run_expect 0 "$WORK/ingest.json" "${MINIVULN[@]}" ingest "$WORK/trivy-old.json" "$WORK/grype-old.json" --json
"${CHECK[@]}" dedupe "$WORK/trivy-old.json" "$WORK/grype-old.json" "$WORK/ingest.json" || fail "deduplication"

echo "▸ CVSS scores in the reports against minivuln's computation from the same vectors"
"${CHECK[@]}" claims "$WORK/trivy-old.json" "$WORK/grype-old.json" "$WORK/claims" || fail "collect CVSS claims"
run_any "0 1" "$WORK/claims-scored.json" "${MINIVULN[@]}" cvss --file "$WORK/claims.txt" --json
"${CHECK[@]}" scores "$WORK/claims.json" "$WORK/claims-scored.json" || fail "CVSS scores in the reports"

echo "▸ VEX: one vexctl statement for $KEV_CVE, honoured by trivy --vex, grype --vex and minivuln"
"${CHECK[@]}" purls "$WORK/trivy-old.json" "$KEV_CVE" > "$WORK/purls.txt" || fail "find the $KEV_CVE packages: $(cat "$WORK/purls.txt")"
PRODUCTS=()
while IFS= read -r p; do [[ -n "$p" ]] && PRODUCTS+=(--product "$p"); done < "$WORK/purls.txt"
echo "  products: $(tr '\n' ' ' < "$WORK/purls.txt")"
run_expect 0 "$WORK/vexctl.txt" vexctl create --author "cc-smoke@example.com" "${PRODUCTS[@]}" --vuln "$KEV_CVE" --status not_affected \
  --justification vulnerable_code_cannot_be_controlled_by_adversary --impact-statement "lab only: mechanics test, not a real assessment" --file "$WORK/vex.json"
run_expect 0 "$WORK/vex-lint.txt" "${MINIVULN[@]}" vex "$WORK/vex.json"
grep -q "$KEV_CVE" "$WORK/vex-lint.txt" || fail "minivuln should read vexctl's document: $(cat "$WORK/vex-lint.txt")"
run_expect 0 "$WORK/trivy-vex.log.txt" trivy image --quiet --scanners vuln --vex "$WORK/vex.json" --show-suppressed --format json --output "$WORK/trivy-old-vex.json" "$OLD_TAG"
run_expect 0 "$WORK/grype-vex.log.txt" grype "docker:$OLD_TAG" -q --vex "$WORK/vex.json" -o json --file "$WORK/grype-old-vex.json"
run_expect 0 "$WORK/rank-vex.json" "${MINIVULN[@]}" rank "$WORK/trivy-old.json" "$WORK/grype-old.json" "${FEEDS[@]}" --vex "$WORK/vex.json" --exposure internet --json
"${CHECK[@]}" vex "$WORK/trivy-old.json" "$WORK/trivy-old-vex.json" "$WORK/grype-old.json" "$WORK/grype-old-vex.json" "$WORK/rank-vex.json" "$KEV_CVE" || fail "VEX suppression"

echo "▸ ranking with the KEV and EPSS fixtures: the KEV entry comes first"
run_expect 0 "$WORK/rank.json" "${MINIVULN[@]}" rank "$WORK/trivy-old.json" "$WORK/grype-old.json" "${FEEDS[@]}" --exposure internet --json
"${CHECK[@]}" top "$WORK/rank.json" "$F/kev-2026-10-30.json" "$KEV_CVE" || fail "ranking"
run_expect 1 "$WORK/gate.txt" "${MINIVULN[@]}" rank "$WORK/trivy-old.json" "${FEEDS[@]}" --fail-on P1

echo "▸ diff against the newer tag: fixed findings disappear"
run_any "0 1" "$WORK/diff.json" "${MINIVULN[@]}" diff --old "$WORK/trivy-old.json" --new "$WORK/trivy-new.json" "${FEEDS[@]}" --exposure internet --json
"${CHECK[@]}" diff "$WORK/trivy-old.json" "$WORK/trivy-new.json" "$WORK/diff.json" "$KEV_CVE" || fail "diff"
run_any "0 1" "$WORK/diff.txt" "${MINIVULN[@]}" diff --old "$WORK/trivy-old.json" "$WORK/grype-old.json" --new "$WORK/trivy-new.json"
grep -q "^warning: the old side was scanned with grype, trivy" "$WORK/diff.txt" || fail "a diff across different tool sets should warn: $(head -n 3 "$WORK/diff.txt")"

echo "▸ live feeds (optional): today's KEV catalogue and EPSS scores"
if curl -fsSL --retry 2 -m 60 -A "cloudcaptain-lesson-smoke" -o "$WORK/kev-live.json" https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json 2> "$WORK/kev-live.err" \
  && curl -fsSL --retry 2 -m 120 -o "$WORK/epss-live.csv.gz" https://epss.empiricalsecurity.com/epss_scores-current.csv.gz 2> "$WORK/epss-live.err"; then
  run_expect 0 "$WORK/rank-live.json" "${MINIVULN[@]}" rank "$WORK/trivy-old.json" "$WORK/grype-old.json" --kev "$WORK/kev-live.json" --epss "$WORK/epss-live.csv.gz" --exposure internet --json
  "${CHECK[@]}" live "$WORK/rank-live.json" "$WORK/grype-old.json" "$KEV_CVE" || fail "live ranking"
else
  echo "  (live KEV or EPSS feed not reachable: $(cat "$WORK/kev-live.err" "$WORK/epss-live.err" 2>/dev/null | head -n 2 | tr '\n' ' '); skipped)"
fi

echo "SMOKE OK: security/vuln-prioritization"
