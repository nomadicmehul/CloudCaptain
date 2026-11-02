#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
PKI="$WORK/pki"
PIDS=()
cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do { kill "$pid"; wait "$pid"; } 2>/dev/null; done
  rm -rf "$WORK"
}
trap cleanup EXIT

command -v openssl >/dev/null || fail "openssl is required"
tl() { python3 code/main.py "$@"; }
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }
wait_for() { for _ in $(seq 1 100); do grep -q "$2" "$1" 2>/dev/null && return 0; sleep 0.1; done; fail "no '$2' in $1: $(cat "$1" 2>/dev/null)"; }

cat > "$WORK/check.py" <<'PY'
import json
import re
import sys


def load(path):
    with open(path, errors="replace") as handle:
        return handle.read()


def first(kind, path):
    text = load(path)
    if kind == "tlslens":
        errors = json.loads(text)["errors"]
        return f"{errors[0]['code']}@{errors[0]['depth']}" if errors else "0"
    if kind == "verify":
        match = re.search(r"error (\d+) at (\d+) depth lookup", text)
        if match:
            return f"{match.group(1)}@{match.group(2)}"
        return "0" if re.search(r": OK\s*$", text, re.M) else "none"
    depth = None
    for line in text.splitlines():
        found = re.match(r"depth=(\d+)", line)
        if found:
            depth = found.group(1)
        error = re.match(r"verify error:num=(\d+)", line)
        if error:
            return f"{error.group(1)}@{depth}"
    match = re.search(r"Verify return code: (\d+)", text)
    return match.group(1) if match else "none"


def x509(fields_path, san_path, json_path):
    fields = dict(line.split("=", 1) for line in load(fields_path).splitlines() if "=" in line)
    ours = json.loads(load(json_path))["certificates"][0]
    problems = []
    if int(fields["serial"], 16) != int(ours["serial"], 16):
        problems.append(f"serial {fields['serial']} vs {ours['serial']}")
    fingerprint = next(v for k, v in fields.items() if k.lower().endswith("fingerprint"))
    if fingerprint.upper() != ours["sha256_fingerprint"]:
        problems.append(f"fingerprint {fingerprint} vs {ours['sha256_fingerprint']}")
    if fields["subject"] != ours["subject_rfc4514"]:
        problems.append(f"subject {fields['subject']} vs {ours['subject_rfc4514']}")
    lines = load(san_path).splitlines()
    index = next(i for i, line in enumerate(lines) if "Subject Alternative Name" in line)
    san = [s.strip() for s in lines[index + 1].split(",")]
    mine = [f"DNS:{n}" for n in ours["dns_names"]] + [f"IP Address:{a}" for a in ours["ip_addresses"]]
    if san != mine:
        problems.append(f"SAN {san} vs {mine}")
    return problems


def hello(listen_json, sni, alpn, curl_path=None):
    client = json.loads(load(listen_json))["client"]["hellos"][0]
    problems = []
    if client["sni"] != sni:
        problems.append(f"SNI {client['sni']} vs {sni}")
    if client["alpn"] != alpn.split(","):
        problems.append(f"ALPN {client['alpn']} vs {alpn}")
    if curl_path:
        offered = []
        for value in re.findall(r"ALPN[:,] (?:curl )?offer(?:s|ing) (\S+)", load(curl_path)):
            offered.extend(value.split(","))
        if offered and offered != client["alpn"]:
            problems.append(f"curl -v says it offered {offered}, the ClientHello carried {client['alpn']}")
    return problems


def relay(relay_json, sclient_path, ciphers_path, fingerprints):
    server = json.loads(load(relay_json))["server"]
    text = load(sclient_path)
    new = re.search(r"New, (TLSv1\.[23]), Cipher is (\S+)", text)
    alpn = re.search(r"ALPN protocol: (\S+)", text)
    if not new:
        return [f"no 'New, TLSv1.x, Cipher is' line from s_client: {text[-600:]}"]
    hello = server["hellos"][0]
    problems = []
    version = new.group(1).replace("TLSv", "TLS ")
    if hello["version"] != version:
        problems.append(f"version {hello['version']} vs s_client {version}")
    codes = {}
    for line in load(ciphers_path).splitlines():
        match = re.search(r"0x([0-9A-Fa-f]{2}),0x([0-9A-Fa-f]{2}) - (\S+)", line)
        if match:
            codes[match.group(3)] = int(match.group(1) + match.group(2), 16)
    if codes.get(new.group(2)) != hello["cipher_suite_id"]:
        problems.append(f"cipher {hello['cipher_suite']} ({hello['cipher_suite_id']:#06x}) vs s_client {new.group(2)} ({codes.get(new.group(2))})")
    if version == "TLS 1.3":
        if hello["alpn"] is not None or server["certificates"]:
            problems.append("TLS 1.3 ServerHello should carry neither ALPN nor certificates")
    else:
        if not alpn or hello["alpn"] != alpn.group(1):
            problems.append(f"ALPN {hello['alpn']} vs s_client {alpn.group(1) if alpn else None}")
        seen = [c["sha256_fingerprint"] for c in server["certificates"]]
        if seen != fingerprints:
            problems.append(f"chain {seen} vs {fingerprints}")
    return problems


def fingerprint(json_path, index):
    data = json.loads(load(json_path))
    return data["certificates"][int(index)]["sha256_fingerprint"]


command, args = sys.argv[1], sys.argv[2:]
if command == "first":
    print(first(*args))
elif command == "fingerprint":
    print(fingerprint(*args))
else:
    handler = {"x509": x509, "hello": hello, "relay": lambda j, s, c, *f: relay(j, s, c, list(f))}[command]
    issues = handler(*args)
    for issue in issues:
        print(issue)
    sys.exit(1 if issues else 0)
PY
check() { python3 "$WORK/check.py" "$@"; }

cat > "$WORK/pyssl.py" <<'PY'
import socket
import ssl
import sys

port, host, cafile = int(sys.argv[1]), sys.argv[2], sys.argv[3]
context = ssl.create_default_context(cafile=cafile)
try:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            print(0, tls.version(), tls.cipher()[0])
except ssl.SSLCertVerificationError as exc:
    print(exc.verify_code, exc.verify_message)
PY

echo "▸ a lab PKI made with openssl: root, intermediate, leaf, expired, wrong SAN, untrusted root, ECDSA"
echo "  $(openssl version)"
mkdir -p "$PKI/newcerts"
: > "$PKI/index.txt"
echo 1000 > "$PKI/serial"
cat > "$PKI/pki.cnf" <<'CNF'
[ ca ]
default_ca = lab
[ lab ]
dir = .
database = ./index.txt
new_certs_dir = ./newcerts
serial = ./serial
default_md = sha256
policy = any_name
unique_subject = no
preserve = yes
email_in_dn = no
[ any_name ]
countryName = optional
organizationName = optional
commonName = supplied
[ req ]
distinguished_name = dn
prompt = no
[ dn ]
CN = placeholder
[ root_ext ]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
[ inter_ext ]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
[ leaf_ext ]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
subjectAltName = $ENV::SAN
CNF
stamp() { python3 -c 'import datetime as d, sys; print((d.datetime.now(d.timezone.utc) + d.timedelta(days=int(sys.argv[1]))).strftime("%Y%m%d%H%M%SZ"))' "$1"; }
START="$(stamp -1)"
LEAF_END="$(stamp 90)"
CA_END="$(stamp 3650)"
export SAN="DNS:unused"
mk() {
  local name="$1" alg="$2" subject="$3" issuer="$4" ext="$5" start="$6" end="$7" md="${8:-sha256}"
  if [[ "$alg" == rsa ]]; then
    openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$PKI/$name.key" 2>/dev/null || fail "RSA key for $name"
  else
    openssl genpkey -algorithm EC -pkeyopt "ec_paramgen_curve:$alg" -out "$PKI/$name.key" 2>/dev/null || fail "EC key for $name"
  fi
  openssl req -new -config "$PKI/pki.cnf" -key "$PKI/$name.key" -subj "$subject" -out "$PKI/$name.csr" || fail "CSR for $name"
  local signer=(-cert "$PKI/$issuer.pem" -keyfile "$PKI/$issuer.key")
  if [[ "$issuer" == self ]]; then signer=(-selfsign -keyfile "$PKI/$name.key"); fi
  (cd "$PKI" && openssl ca -batch -notext -config pki.cnf "${signer[@]}" -in "$name.csr" -out "$name.pem" -extensions "$ext" -startdate "$start" -enddate "$end" -md "$md" > "$name.ca.txt" 2>&1) || fail "openssl ca for $name: $(cat "$PKI/$name.ca.txt")"
}
mk root rsa "/C=DE/O=CloudCaptain Lab/CN=CloudCaptain Lab Root" self root_ext "$START" "$CA_END"
mk inter rsa "/C=DE/O=CloudCaptain Lab/CN=CloudCaptain Lab Intermediate" root inter_ext "$START" "$CA_END"
SAN="DNS:app.cc.test,DNS:*.api.cc.test,IP:127.0.0.1" mk leaf rsa "/CN=app.cc.test" inter leaf_ext "$START" "$LEAF_END"
SAN="DNS:expired.cc.test" mk expired rsa "/CN=expired.cc.test" inter leaf_ext 20250101000000Z 20250201000000Z
SAN="DNS:other.cc.test" mk wrong-san rsa "/CN=app.cc.test" inter leaf_ext "$START" "$LEAF_END"
mk evil-root rsa "/O=Intercepting Proxy/CN=Untrusted Root" self root_ext "$START" "$CA_END"
SAN="DNS:app.cc.test" mk evil-leaf rsa "/CN=app.cc.test" evil-root leaf_ext "$START" "$LEAF_END"
mk ec-inter P-384 "/C=DE/O=CloudCaptain Lab/CN=CloudCaptain Lab ECC Intermediate" root inter_ext "$START" "$CA_END"
SAN="DNS:ec.cc.test" mk ec-leaf P-256 "/CN=ec.cc.test" ec-inter leaf_ext "$START" "$LEAF_END" sha384
echo "  $(ls "$PKI"/*.pem | wc -l | tr -d ' ') certificates in $PKI"

echo "▸ tlslens x509 reads the same serial, fingerprint, subject and SANs as openssl x509"
for name in leaf ec-leaf; do
  openssl x509 -in "$PKI/$name.pem" -noout -serial -fingerprint -sha256 -subject -nameopt RFC2253 > "$WORK/$name.x509.txt"
  openssl x509 -in "$PKI/$name.pem" -noout -ext subjectAltName > "$WORK/$name.san.txt"
  tl x509 "$PKI/$name.pem" --json > "$WORK/$name.x509.json" || fail "tlslens x509 $name"
  check x509 "$WORK/$name.x509.txt" "$WORK/$name.san.txt" "$WORK/$name.x509.json" > "$WORK/$name.x509.diff" || fail "$name: $(cat "$WORK/$name.x509.diff")"
  echo "  $name: $(grep -i fingerprint "$WORK/$name.x509.txt" | cut -c1-40)..."
done
LEAF_FP="$(check fingerprint "$WORK/leaf.x509.json" 0)"
EC_FP="$(check fingerprint "$WORK/ec-leaf.x509.json" 0)"
tl x509 "$PKI/inter.pem" --json > "$WORK/inter.x509.json"
INTER_FP="$(check fingerprint "$WORK/inter.x509.json" 0)"

echo "▸ openssl verify and tlslens verify report the same first error, code and depth"
file_case() {
  local name="$1" leaf="$2" host="$3" expect="$4"
  shift 4
  local theirs=() ours=()
  for extra in "$@"; do theirs+=(-untrusted "$PKI/$extra.pem"); ours+=(--untrusted "$PKI/$extra.pem"); done
  openssl verify -purpose sslserver -CAfile "$PKI/root.pem" ${theirs[@]+"${theirs[@]}"} -verify_hostname "$host" "$PKI/$leaf.pem" > "$WORK/$name.verify.txt" 2>&1 || true
  tl verify "$PKI/$leaf.pem" --CAfile "$PKI/root.pem" ${ours[@]+"${ours[@]}"} --host "$host" --json > "$WORK/$name.file.json" || true
  local want got
  want="$(check first verify "$WORK/$name.verify.txt")"
  got="$(check first tlslens "$WORK/$name.file.json")"
  [[ "$want" == "$got" ]] || fail "$name: openssl verify says $want, tlslens says $got: $(cat "$WORK/$name.verify.txt") $(cat "$WORK/$name.file.json")"
  [[ "${got%@*}" == "$expect" ]] || fail "$name: expected error $expect, both said $got: $(cat "$WORK/$name.verify.txt")"
  echo "  $name: $got  ($(grep -m1 -E 'error [0-9]+ at|: OK' "$WORK/$name.verify.txt"))"
}
file_case valid leaf app.cc.test 0 inter
file_case wildcard leaf orders.api.cc.test 0 inter
file_case expired expired expired.cc.test 10 inter
file_case wrong-san wrong-san app.cc.test 62 inter
file_case missing-intermediate leaf app.cc.test 20
file_case untrusted-root evil-leaf app.cc.test 19 evil-root
file_case ecdsa ec-leaf ec.cc.test 0 ec-inter

echo "▸ the same leaves served by openssl s_server, checked by s_client, Python ssl, curl and tlslens --connect"
serve() {
  local name="$1" leafname="$2" chain="$3"
  shift 3
  local port extra=()
  port="$(free_port)"
  if [[ -n "$chain" ]]; then extra=(-cert_chain "$PKI/$chain.pem"); fi
  openssl s_server -accept "127.0.0.1:$port" -cert "$PKI/$leafname.pem" -key "$PKI/$leafname.key" ${extra[@]+"${extra[@]}"} -alpn h2,http/1.1 -www "$@" < /dev/null > "$WORK/$name.server.txt" 2>&1 &
  PIDS+=("$!")
  echo "$port" > "$WORK/$name.port"
}
serve good leaf inter
serve expired expired inter
serve wrong-san wrong-san inter
serve untrusted-root evil-leaf evil-root
serve no-intermediate leaf ""
serve ecdsa ec-leaf ec-inter
serve sni leaf inter -servername ec.cc.test -cert2 "$PKI/ec-leaf.pem" -key2 "$PKI/ec-leaf.key"
for name in good expired wrong-san untrusted-root no-intermediate ecdsa sni; do wait_for "$WORK/$name.server.txt" ACCEPT; done
server_case() {
  local name="$1" host="$2" expect="$3" port
  port="$(cat "$WORK/$name.port")"
  openssl s_client -connect "127.0.0.1:$port" -servername "$host" -verify_hostname "$host" -CAfile "$PKI/root.pem" < /dev/null > "$WORK/$name.sclient.txt" 2>&1 || true
  tl verify --connect "127.0.0.1:$port" --servername "$host" --CAfile "$PKI/root.pem" --json > "$WORK/$name.connect.json" || true
  python3 "$WORK/pyssl.py" "$port" "$host" "$PKI/root.pem" > "$WORK/$name.py.txt" 2>&1 || true
  local sc ours py
  sc="$(check first s_client "$WORK/$name.sclient.txt")"
  ours="$(check first tlslens "$WORK/$name.connect.json")" || fail "$name: tlslens verify --connect: $(cat "$WORK/$name.connect.json")"
  py="$(cut -d' ' -f1 "$WORK/$name.py.txt")"
  [[ "$ours" == "$sc" ]] || fail "$name: s_client says $sc, tlslens says $ours: $(grep -E 'verify error|Verify return' "$WORK/$name.sclient.txt")"
  [[ "${ours%@*}" == "$py" ]] || fail "$name: Python ssl says $(cat "$WORK/$name.py.txt"), tlslens says $ours"
  [[ "${ours%@*}" == "$expect" ]] || fail "$name: expected error $expect, all three said $ours"
  local curl_note=""
  if command -v curl >/dev/null; then
    local code=0
    curl -s -o /dev/null --http1.1 --max-time 10 --cacert "$PKI/root.pem" --resolve "$host:$port:127.0.0.1" "https://$host:$port/" || code=$?
    if [[ "$expect" == 0 ]]; then [[ "$code" == 0 ]] || fail "$name: curl exit $code, expected 0"; else [[ "$code" == 60 ]] || fail "$name: curl exit $code, expected 60 (peer certificate cannot be authenticated)"; fi
    curl_note=", curl exit $code"
  fi
  echo "  $name: s_client $sc, Python ssl $(cat "$WORK/$name.py.txt")$curl_note, tlslens $ours"
}
server_case good app.cc.test 0
server_case expired expired.cc.test 10
server_case wrong-san app.cc.test 62
server_case untrusted-root app.cc.test 19
server_case no-intermediate app.cc.test 20
server_case ecdsa ec.cc.test 0
grep -q "Verify return code: 21" "$WORK/no-intermediate.sclient.txt" || fail "s_client should continue past 20 and end with 21: $(grep 'Verify return' "$WORK/no-intermediate.sclient.txt")"
python3 -c 'import json, sys; codes = [e["code"] for e in json.load(open(sys.argv[1]))["errors"]]; sys.exit(0 if codes == [20, 21] else "tlslens errors %s, expected [20, 21]" % codes)' "$WORK/no-intermediate.connect.json"
echo "  no-intermediate: s_client and tlslens both continue past 20 and report 21, exactly what a server that forgot its intermediate looks like"

echo "▸ SNI picks the certificate: one s_server, two names, two chains"
SNI_PORT="$(cat "$WORK/sni.port")"
tl probe "127.0.0.1:$SNI_PORT" --servername app.cc.test --json > "$WORK/sni-app.json" || fail "probe app.cc.test: $(cat "$WORK/sni-app.json")"
tl probe "127.0.0.1:$SNI_PORT" --servername ec.cc.test --json > "$WORK/sni-ec.json" || fail "probe ec.cc.test: $(cat "$WORK/sni-ec.json")"
[[ "$(check fingerprint "$WORK/sni-app.json" 0)" == "$LEAF_FP" ]] || fail "SNI app.cc.test should get the RSA leaf"
[[ "$(check fingerprint "$WORK/sni-ec.json" 0)" == "$EC_FP" ]] || fail "SNI ec.cc.test should get the ECDSA leaf"
openssl s_client -connect "127.0.0.1:$SNI_PORT" -servername ec.cc.test -showcerts < /dev/null > "$WORK/sni-showcerts.txt" 2>&1 || true
tl x509 "$WORK/sni-showcerts.txt" --json > "$WORK/sni-showcerts.json" || fail "tlslens x509 on s_client -showcerts output: $(head -20 "$WORK/sni-showcerts.txt")"
[[ "$(check fingerprint "$WORK/sni-showcerts.json" 0)" == "$EC_FP" ]] || fail "s_client -showcerts with SNI ec.cc.test should show the ECDSA leaf"
tl verify "$WORK/sni-showcerts.txt" --untrusted "$PKI/ec-inter.pem" --CAfile "$PKI/root.pem" --host ec.cc.test > "$WORK/sni-verify.txt" || fail "verify the -showcerts output: $(cat "$WORK/sni-verify.txt")"
echo "  app.cc.test -> RSA leaf, ec.cc.test -> P-256 leaf; s_client -showcerts output verifies with tlslens"

echo "▸ ClientHellos recorded by the tlslens listener: openssl s_client and curl"
start_listener() {
  local tag="$1"
  shift
  tl listen --port 0 --out "$WORK/$tag.client.bin" --wait 30 --json "$@" > "$WORK/$tag.listen.json" 2> "$WORK/$tag.listen.err" &
  LISTEN_PID=$!
  PIDS+=("$LISTEN_PID")
  wait_for "$WORK/$tag.listen.err" "listening on"
  LISTEN_PORT="$(sed -n 's/^listening on 127.0.0.1:\([0-9]*\)$/\1/p' "$WORK/$tag.listen.err")"
}
start_listener openssl-hello
openssl s_client -connect "127.0.0.1:$LISTEN_PORT" -servername app.cc.test -alpn h2,http/1.1 < /dev/null > "$WORK/openssl-hello.sclient.txt" 2>&1 || true
wait "$LISTEN_PID" || fail "listener: $(cat "$WORK/openssl-hello.listen.err")"
grep -qiE "alert number 40|handshake failure" "$WORK/openssl-hello.sclient.txt" || fail "s_client should report the handshake_failure alert: $(tail -5 "$WORK/openssl-hello.sclient.txt")"
check hello "$WORK/openssl-hello.listen.json" app.cc.test h2,http/1.1 > "$WORK/openssl-hello.diff" || fail "s_client ClientHello: $(cat "$WORK/openssl-hello.diff")"
tl hello "$WORK/openssl-hello.client.bin" > "$WORK/openssl-hello.txt"
grep -E "offers|key shares" "$WORK/openssl-hello.txt" | sed 's/^/  /'
if command -v curl >/dev/null; then
  start_listener curl-hello
  CURL_CODE=0
  curl -sv --http1.1 --max-time 10 --resolve "app.cc.test:$LISTEN_PORT:127.0.0.1" "https://app.cc.test:$LISTEN_PORT/" > /dev/null 2> "$WORK/curl-hello.curl.txt" || CURL_CODE=$?
  wait "$LISTEN_PID" || fail "listener: $(cat "$WORK/curl-hello.listen.err")"
  [[ "$CURL_CODE" != 0 ]] || fail "curl should fail against a listener that answers with an alert"
  check hello "$WORK/curl-hello.listen.json" app.cc.test http/1.1 "$WORK/curl-hello.curl.txt" > "$WORK/curl-hello.diff" || fail "curl ClientHello: $(cat "$WORK/curl-hello.diff")"
  echo "  curl (exit $CURL_CODE): SNI app.cc.test, ALPN http/1.1, as curl -v says it offered"
else
  echo "  (curl not installed; curl ClientHello skipped)"
fi

echo "▸ through the tlslens relay: TLS 1.3 encrypts ALPN and the chain, TLS 1.2 shows both"
openssl ciphers -V ALL > "$WORK/ciphers.txt" 2>/dev/null || openssl ciphers -V > "$WORK/ciphers.txt"
GOOD_PORT="$(cat "$WORK/good.port")"
for version in 1.3 1.2; do
  tag="relay-$version"
  flag=()
  if [[ "$version" == 1.2 ]]; then flag=(-tls1_2); fi
  start_listener "$tag" --upstream "127.0.0.1:$GOOD_PORT" --server-out "$WORK/$tag.server.bin"
  openssl s_client -connect "127.0.0.1:$LISTEN_PORT" -servername app.cc.test -alpn h2,http/1.1 -CAfile "$PKI/root.pem" ${flag[@]+"${flag[@]}"} < /dev/null > "$WORK/$tag.sclient.txt" 2>&1 || true
  wait "$LISTEN_PID" || fail "relay listener: $(cat "$WORK/$tag.listen.err")"
  check relay "$WORK/$tag.listen.json" "$WORK/$tag.sclient.txt" "$WORK/ciphers.txt" "$LEAF_FP" "$INTER_FP" > "$WORK/$tag.diff" || fail "TLS $version relay: $(cat "$WORK/$tag.diff")"
  tl hello "$WORK/$tag.server.bin" > "$WORK/$tag.txt" || fail "tlslens hello on the server bytes"
  echo "  TLS $version: $(grep -m1 -E '^server_hello' "$WORK/$tag.txt"); s_client: $(grep -m1 -E '^ALPN protocol' "$WORK/$tag.sclient.txt" || echo 'no ALPN line')"
done

echo "SMOKE OK: networking/tls-handshake"
