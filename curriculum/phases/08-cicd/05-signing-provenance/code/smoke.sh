#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

LESSON="$PWD"
FIX="$LESSON/code/fixtures"
COSIGN_VERSION="3.1.4"
BUILDER="https://ci.example/builders/release@v1"
SOURCE="git+https://git.example/acme/app@refs/heads/main"
COMMIT="1f0c3e5d9a7b2c4e6f8a0b1c2d3e4f5a6b7c8d9e"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
mkdir -p "$WORK/bin" "$WORK/dl" "$WORK/run"
export PATH="$WORK/bin:$PATH"

m() { python3 "$LESSON/code/main.py" "$@"; }
sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }
run_expect() {
  local want="$1" out="$2" got=0
  shift 2
  "$@" > "$out" 2>&1 || got=$?
  [[ "$got" == "$want" ]] || fail "exit $got (want $want) from: $*
$(cat "$out")"
}
expect_fail() {
  local out="$1" got=0
  shift
  "$@" > "$out" 2>&1 || got=$?
  [[ "$got" != "0" ]] || fail "should have failed: $*
$(cat "$out")"
}
assert_report() {
  python3 - "$@" <<'PY' || fail "report $1 is not what the lesson says"
import json, sys
path, want, passed, failed = sys.argv[1:5]
report = json.load(open(path))
status = {c["name"]: c["ok"] for c in report["checks"]}
problems = []
if report["ok"] is not (want == "true"):
    problems.append("ok is %s, want %s" % (report["ok"], want))
problems += ["%s should pass, got %s" % (n, status.get(n)) for n in filter(None, passed.split(",")) if status.get(n) is not True]
problems += ["%s should fail, got %s" % (n, status.get(n)) for n in filter(None, failed.split(",")) if status.get(n) is not False]
if problems:
    sys.exit("; ".join(problems) + "\n" + json.dumps(report, indent=2))
print("  %s: %s" % (report["subject"], ", ".join("%s=%s" % (c["name"], {True: "pass", False: "FAIL", None: "skip"}[c["ok"]]) for c in report["checks"])))
PY
}

command -v openssl >/dev/null || fail "openssl is required"
S="$FIX/sigstore"
echo "▸ minisign on real Sigstore data: a staging Rekor bundle verifies offline (signature, identity, promise, inclusion proof, checkpoint)"
REAL=(m verify-blob --json --bundle "$S/bundle_v3.txt.sigstore.json" --certificate-identity william@yossarian.net --certificate-oidc-issuer https://github.com/login/oauth)
run_expect 0 "$WORK/real.json" "${REAL[@]}" --insecure-ignore-chain --log-key "$S/rekor-staging.pub" "$S/bundle_v3.txt"
assert_report "$WORK/real.json" true signature,message-digest,identity,issuer,tlog-promise,tlog-inclusion,tlog-checkpoint,tlog-entry-matches ""
run_expect 1 "$WORK/real-strict.json" "${REAL[@]}" --log-key "$S/rekor-staging.pub" "$S/bundle_v3.txt"
assert_report "$WORK/real-strict.json" false signature,tlog-inclusion certificate-chain
run_expect 1 "$WORK/real-wrong-log.json" "${REAL[@]}" --insecure-ignore-chain --log-key "$S/rekor-public-good.pub" "$S/bundle_v3.txt"
assert_report "$WORK/real-wrong-log.json" false tlog-inclusion tlog-log-id,tlog-checkpoint

echo "▸ the same Rekor checkpoint by hand: three note lines, then a 4-byte key hint and a DER signature that openssl verifies"
python3 - "$S/bundle_v3.txt.sigstore.json" "$WORK/checkpoint.txt" <<'PY'
import json, sys
entry = json.load(open(sys.argv[1]))["verificationMaterial"]["tlogEntries"][0]
open(sys.argv[2], "w").write(entry["inclusionProof"]["checkpoint"]["envelope"])
PY
head -3 "$WORK/checkpoint.txt" > "$WORK/checkpoint-body.txt"
sed -n '5p' "$WORK/checkpoint.txt" | awk '{print $NF}' | base64 -d | tail -c +5 > "$WORK/checkpoint-sig.der"
openssl dgst -sha256 -verify "$S/rekor-staging.pub" -signature "$WORK/checkpoint-sig.der" "$WORK/checkpoint-body.txt" > "$WORK/ossl-checkpoint.txt" 2>&1 || fail "openssl rejects Rekor's checkpoint signature: $(cat "$WORK/ossl-checkpoint.txt")"
grep -q "Verified OK" "$WORK/ossl-checkpoint.txt" || fail "openssl output: $(cat "$WORK/ossl-checkpoint.txt")"
echo "  openssl: $(cat "$WORK/ossl-checkpoint.txt") ($(sed -n '1p' "$WORK/checkpoint.txt"), tree size $(sed -n '2p' "$WORK/checkpoint.txt"))"

cd "$WORK/run"
printf 'app 1.4.2\n' > app.tar.gz
printf 'app 1.4.3\n' > other.tar.gz
echo "▸ minisign and openssl: each verifies the other's ECDSA P-256 signatures and keys"
m keygen --output-key-prefix mini > keygen.txt
m sign-blob --key mini.key --bundle mini.sigstore.json --output-signature mini.sig app.tar.gz > sign.txt
base64 -d < mini.sig > mini.sig.der
openssl dgst -sha256 -verify mini.pub -signature mini.sig.der app.tar.gz > ossl-verify.txt 2>&1 || fail "openssl rejects minisign's signature: $(cat ossl-verify.txt)"
openssl pkey -in mini.key -pubout -out ossl-mini.pub 2> ossl-pkey.txt || fail "openssl cannot read minisign's PKCS#8 key: $(cat ossl-pkey.txt)"
cmp -s ossl-mini.pub mini.pub || fail "openssl derives a different public key PEM: $(cat ossl-mini.pub mini.pub)"
openssl dgst -sha256 -sign mini.key -out ossl.sig.der app.tar.gz
run_expect 0 verify-ossl.json m verify-blob --json --key mini.pub --signature ossl.sig.der app.tar.gz
assert_report verify-ossl.json true signature ""
if openssl dgst -sha256 -sign mini.key -sigopt nonce-type:1 -out det.der app.tar.gz 2> det.err; then
  cmp -s det.der mini.sig.der || fail "openssl's RFC 6979 signature differs from minisign's: $(base64 < det.der) vs $(cat mini.sig)"
  echo "  RFC 6979: openssl's deterministic signature is byte-identical to minisign's"
else
  echo "  ($(openssl version) has no nonce-type:1, which arrived in OpenSSL 3.2; the RFC 6979 byte comparison is skipped, the unit tests check the RFC vectors)"
fi

echo "▸ minisign provenance, attest and verify-attestation: the policy pins builder, source and subject"
m provenance --subject app.tar.gz --builder-id "$BUILDER" --source "$SOURCE" --commit "$COMMIT" --param workflow=.ci/release.yml --invocation-id https://ci.example/runs/4211 --output statement.json
m attest --key mini.key --statement statement.json --bundle mini.att.sigstore.json
cat > policy.json <<EOF
{"builderId": "$BUILDER", "source": "git+https://git.example/acme/app", "buildType": "https://cloudcaptain.io/learn/cicd/signing-provenance"}
EOF
ATT=(m verify-attestation --json --key mini.pub --bundle mini.att.sigstore.json)
run_expect 0 att-ok.json "${ATT[@]}" --artifact app.tar.gz --policy policy.json
assert_report att-ok.json true envelope-signature,payload-type,statement,predicate-type,subject,builder,build-type,source ""
run_expect 1 att-builder.json "${ATT[@]}" --artifact app.tar.gz --policy policy.json --builder-id https://github.com/acme/app/.github/workflows/release.yml@refs/heads/main
assert_report att-builder.json false subject builder
run_expect 1 att-subject.json "${ATT[@]}" --artifact other.tar.gz --policy policy.json
assert_report att-subject.json false builder subject
run_expect 2 att-nobuilder.txt "${ATT[@]}" --artifact app.tar.gz
grep -q "builderId is required" att-nobuilder.txt || fail "a policy without a builder must be refused: $(cat att-nobuilder.txt)"

echo "▸ minisign's transparency log: hashedrekord and dsse entries, signed checkpoints, inclusion and consistency proofs"
m keygen --output-key-prefix log > /dev/null
m log init --log tlog --key log.key --origin rekor.cc.test > log-init.txt
m log add --log tlog --key log.key --bundle mini.sigstore.json --public-key mini.pub --output mini.tlog.json > add1.txt
m log add --log tlog --key log.key --bundle mini.att.sigstore.json --public-key mini.pub --output att.tlog.json > add2.txt
m sign-blob --key mini.key --bundle other.sigstore.json other.tar.gz > /dev/null
m log add --log tlog --key log.key --bundle other.sigstore.json --public-key mini.pub --output other.tlog.json > add3.txt
run_expect 0 blob-tlog.json m verify-blob --json --key mini.pub --bundle mini.sigstore.json --log-key log.pub --tlog-entry mini.tlog.json app.tar.gz
assert_report blob-tlog.json true signature,tlog-log-id,tlog-promise,tlog-inclusion,tlog-checkpoint,tlog-entry-matches ""
run_expect 0 att-tlog.json "${ATT[@]}" --artifact app.tar.gz --policy policy.json --log-key log.pub --tlog-entry att.tlog.json
assert_report att-tlog.json true envelope-signature,builder,tlog-inclusion,tlog-checkpoint,tlog-entry-matches ""
run_expect 1 swapped.json m verify-blob --json --key mini.pub --bundle mini.sigstore.json --log-key log.pub --tlog-entry other.tlog.json app.tar.gz
assert_report swapped.json false tlog-inclusion,tlog-checkpoint tlog-entry-matches
for old in 0 1 2 3; do
  for new in $(seq "$old" 3); do
    m log consistency --log tlog --old "$old" --new "$new" --output "cons-$old-$new.json" > /dev/null
    run_expect 0 "cons-$old-$new.txt" m log verify-consistency --log-key log.pub --proof "cons-$old-$new.json"
  done
done
echo "  consistency proofs verify for every pair of sizes 0..3"
leaf() { { printf '\000'; cat "$1"; } | openssl dgst -sha256 -binary; }
leaf tlog/entries/00000000.json > h0.bin
leaf tlog/entries/00000001.json > h1.bin
BYHAND="$({ printf '\001'; cat h0.bin h1.bin; } | openssl dgst -sha256 -binary | base64)"
SIGNED="$(sed -n '3p' tlog/checkpoints/2.txt)"
[[ "$BYHAND" == "$SIGNED" ]] || fail "RFC 6962 by hand gives root $BYHAND, the signed checkpoint for size 2 says $SIGNED"
echo "  root of size 2 by hand with openssl, SHA-256(0x01 || SHA-256(0x00 || e0) || SHA-256(0x00 || e1)): $BYHAND"

echo "▸ tamper with leaf 1 on disk: audit, inclusion against the signed checkpoint, and consistency from a remembered checkpoint all fail"
cp tlog/checkpoints/2.txt remembered-2.txt
python3 - tlog/entries/00000001.json <<'PY'
import json, sys
entry = json.load(open(sys.argv[1]))
entry["spec"]["payloadHash"]["value"] = "0" * 64
open(sys.argv[1], "w").write(json.dumps(entry, sort_keys=True, separators=(",", ":")))
PY
run_expect 1 audit.json m log audit --json --log tlog --log-key log.pub
assert_report audit.json false checkpoint-0,checkpoint-1 checkpoint-2,checkpoint-3
m log prove --log tlog --index 1 --output tampered.tlog.json > /dev/null
run_expect 1 tampered.json m log verify --json --log-key log.pub --entry tampered.tlog.json
assert_report tampered.json false tlog-inclusion tlog-checkpoint
m log add --log tlog --key log.key --bundle mini.sigstore.json --public-key mini.pub > add4.txt
m log consistency --log tlog --old 2 --output rewrite.json > /dev/null
run_expect 1 rewrite.check.json m log verify-consistency --json --log-key log.pub --proof rewrite.json --trusted-checkpoint remembered-2.txt
assert_report rewrite.check.json false old-checkpoint,new-checkpoint consistency

if [[ "${CC_SMOKE_NO_COSIGN:-}" == "1" ]]; then
  echo "  (cosign sections skipped: CC_SMOKE_NO_COSIGN=1)"
  echo "SMOKE OK: cicd/signing-provenance (without cosign)"
  exit 0
fi

echo "▸ download cosign v$COSIGN_VERSION and verify its sha256 against cosign_checksums.txt"
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
ASSET="cosign-$OS-$ARCH"
BASE="https://github.com/sigstore/cosign/releases/download/v$COSIGN_VERSION"
curl -fsSL --retry 3 -o "$WORK/dl/$ASSET" "$BASE/$ASSET" || fail "download $BASE/$ASSET"
curl -fsSL --retry 3 -o "$WORK/dl/cosign_checksums.txt" "$BASE/cosign_checksums.txt" || fail "download $BASE/cosign_checksums.txt"
WANT="$(awk -v f="$ASSET" '$2 == f {print $1}' "$WORK/dl/cosign_checksums.txt")"
GOT="$(sha256_of "$WORK/dl/$ASSET")"
[[ -n "$WANT" && "$WANT" == "$GOT" ]] || fail "cosign checksum mismatch: want '$WANT' got '$GOT'"
install -m 0755 "$WORK/dl/$ASSET" "$WORK/bin/cosign"
cosign version > cosign-version.txt 2>&1 || fail "cosign version: $(cat cosign-version.txt)"
grep -q "$COSIGN_VERSION" cosign-version.txt || fail "unexpected cosign: $(cat cosign-version.txt)"
echo "  $(grep -m1 GitVersion cosign-version.txt | tr -s ' ')"

export COSIGN_PASSWORD="cc-smoke-throwaway"
cat > offline-signing.json <<'EOF'
{"mediaType": "application/vnd.dev.sigstore.signingconfig.v0.2+json"}
EOF

echo "▸ cosign generate-key-pair: minisign reads cosign.pub and refuses the encrypted cosign.key"
run_expect 0 cosign-keys.txt cosign generate-key-pair --output-key-prefix cosign
run_expect 0 cosign-pub.json m inspect --json cosign.pub
[[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["algorithm"])' cosign-pub.json)" == "ECDSA P-256" ]] || fail "cosign.pub: $(cat cosign-pub.json)"
run_expect 2 cosign-key.txt m sign-blob --key cosign.key app.tar.gz
grep -q "cosign key" cosign-key.txt || fail "minisign should explain the cosign key format: $(cat cosign-key.txt)"

echo "▸ cosign sign-blob with an offline signing config: cosign, minisign and openssl all verify the bundle"
run_expect 0 cosign-sign.txt cosign sign-blob --yes --key cosign.key --signing-config offline-signing.json --bundle cosign.sigstore.json app.tar.gz
python3 - cosign.sigstore.json cosign.sig.der <<'PY' || fail "cosign's bundle shape: $(cat cosign.sigstore.json)"
import base64, json, sys
bundle = json.load(open(sys.argv[1]))
assert bundle["mediaType"] == "application/vnd.dev.sigstore.bundle.v0.3+json", bundle["mediaType"]
assert "publicKey" in bundle["verificationMaterial"], bundle["verificationMaterial"]
assert not bundle["verificationMaterial"].get("tlogEntries"), "an offline signing config must not upload to Rekor"
assert bundle["messageSignature"]["messageDigest"]["algorithm"] == "SHA2_256"
open(sys.argv[2], "wb").write(base64.b64decode(bundle["messageSignature"]["signature"]))
print("  cosign wrote a v0.3 bundle: publicKey hint, messageSignature, no tlogEntries")
PY
run_expect 0 cosign-verify.txt cosign verify-blob --key cosign.pub --bundle cosign.sigstore.json --insecure-ignore-tlog app.tar.gz
grep -q "Verified OK" cosign-verify.txt || fail "cosign verify-blob: $(cat cosign-verify.txt)"
run_expect 0 mini-on-cosign.json m verify-blob --json --key cosign.pub --bundle cosign.sigstore.json app.tar.gz
assert_report mini-on-cosign.json true signature,message-digest ""
openssl dgst -sha256 -verify cosign.pub -signature cosign.sig.der app.tar.gz > ossl-cosign.txt 2>&1 || fail "openssl rejects cosign's signature: $(cat ossl-cosign.txt)"
run_expect 1 mini-on-cosign-other.json m verify-blob --json --key cosign.pub --bundle cosign.sigstore.json other.tar.gz
assert_report mini-on-cosign-other.json false "" signature,message-digest
expect_fail cosign-other.txt cosign verify-blob --key cosign.pub --bundle cosign.sigstore.json --insecure-ignore-tlog other.tar.gz

echo "▸ minisign sign-blob: cosign verify-blob accepts minisign's bundle and its bare signature with minisign's PEM key"
run_expect 0 cosign-on-mini.txt cosign verify-blob --key mini.pub --bundle mini.sigstore.json --insecure-ignore-tlog app.tar.gz
grep -q "Verified OK" cosign-on-mini.txt || fail "cosign verify-blob on minisign's bundle: $(cat cosign-on-mini.txt)"
run_expect 0 cosign-on-mini-sig.txt cosign verify-blob --key mini.pub --signature mini.sig --insecure-ignore-tlog app.tar.gz
grep -q "Verified OK" cosign-on-mini-sig.txt || fail "cosign verify-blob --signature: $(cat cosign-on-mini-sig.txt)"
expect_fail cosign-on-mini-other.txt cosign verify-blob --key mini.pub --bundle mini.sigstore.json --insecure-ignore-tlog other.tar.gz
expect_fail cosign-on-mini-wrongkey.txt cosign verify-blob --key cosign.pub --bundle mini.sigstore.json --insecure-ignore-tlog app.tar.gz

echo "▸ attestations: cosign accepts minisign's DSSE provenance, minisign's policy accepts cosign's"
run_expect 0 cosign-att-mini.txt cosign verify-blob-attestation --key mini.pub --bundle mini.att.sigstore.json --type slsaprovenance1 --insecure-ignore-tlog app.tar.gz
grep -q "Verified OK" cosign-att-mini.txt || fail "cosign verify-blob-attestation on minisign's envelope: $(cat cosign-att-mini.txt)"
expect_fail cosign-att-mini-other.txt cosign verify-blob-attestation --key mini.pub --bundle mini.att.sigstore.json --type slsaprovenance1 --insecure-ignore-tlog other.tar.gz
python3 -c 'import json,sys; json.dump(json.load(open(sys.argv[1]))["predicate"], open(sys.argv[2], "w"), indent=2)' statement.json predicate.json
run_expect 0 cosign-attest.txt cosign attest-blob --yes --key cosign.key --predicate predicate.json --type slsaprovenance1 --signing-config offline-signing.json --bundle cosign.att.sigstore.json app.tar.gz
run_expect 0 cosign-att-self.txt cosign verify-blob-attestation --key cosign.pub --bundle cosign.att.sigstore.json --type slsaprovenance1 --insecure-ignore-tlog app.tar.gz
run_expect 0 mini-att-cosign.json m verify-attestation --json --key cosign.pub --bundle cosign.att.sigstore.json --artifact app.tar.gz --policy policy.json
assert_report mini-att-cosign.json true envelope-signature,statement,subject,builder,build-type,source ""
run_expect 0 cosign-att-inspect.json m inspect --json cosign.att.sigstore.json
python3 - cosign-att-inspect.json "$BUILDER" <<'PY' || fail "cosign's statement: $(cat cosign-att-inspect.json)"
import json, sys
summary = json.load(open(sys.argv[1]))
statement = summary["statement"]
assert statement["_type"] in ("https://in-toto.io/Statement/v0.1", "https://in-toto.io/Statement/v1"), statement["_type"]
assert statement["subject"][0]["name"] == "app.tar.gz", statement["subject"]
assert summary["facts"]["builder"] == sys.argv[2], summary["facts"]
print("  cosign wrapped the predicate in a Statement with _type %s and subject %s" % (statement["_type"], statement["subject"][0]["name"]))
PY
run_expect 0 cosign-stmt.txt cosign attest-blob --yes --key cosign.key --statement statement.json --signing-config offline-signing.json --bundle cosign.stmt.sigstore.json app.tar.gz
python3 - cosign.stmt.sigstore.json statement.json <<'PY' || fail "cosign attest-blob --statement must sign the statement bytes unchanged"
import base64, json, sys
payload = base64.b64decode(json.load(open(sys.argv[1]))["dsseEnvelope"]["payload"])
assert payload == open(sys.argv[2], "rb").read()
print("  cosign attest-blob --statement signed minisign's statement byte for byte")
PY
run_expect 0 mini-stmt-cosign.json m verify-attestation --json --key cosign.pub --bundle cosign.stmt.sigstore.json --artifact app.tar.gz --policy policy.json
assert_report mini-stmt-cosign.json true envelope-signature,subject,builder ""

echo "▸ cosign's bundles in minisign's log: the hashedrekord and dsse entries Rekor would record, proven offline"
m log init --log tlog2 --key log.key --origin rekor.cc.test > /dev/null
m log add --log tlog2 --key log.key --bundle cosign.sigstore.json --public-key cosign.pub --output cosign.tlog.json > /dev/null
m log add --log tlog2 --key log.key --bundle cosign.att.sigstore.json --public-key cosign.pub --output cosign-att.tlog.json > /dev/null
run_expect 0 cosign-blob-tlog.json m verify-blob --json --key cosign.pub --bundle cosign.sigstore.json --log-key log.pub --tlog-entry cosign.tlog.json app.tar.gz
assert_report cosign-blob-tlog.json true signature,tlog-inclusion,tlog-checkpoint,tlog-entry-matches ""
run_expect 0 cosign-att-tlog.json m verify-attestation --json --key cosign.pub --bundle cosign.att.sigstore.json --artifact app.tar.gz --policy policy.json --log-key log.pub --tlog-entry cosign-att.tlog.json
assert_report cosign-att-tlog.json true envelope-signature,tlog-inclusion,tlog-checkpoint,tlog-entry-matches ""
run_expect 0 audit2.json m log audit --json --log tlog2 --log-key log.pub
assert_report audit2.json true checkpoint-0,checkpoint-1,checkpoint-2 ""

echo "SMOKE OK: cicd/signing-provenance"
