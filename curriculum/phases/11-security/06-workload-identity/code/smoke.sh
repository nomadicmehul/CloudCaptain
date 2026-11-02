#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

LESSON="$PWD"
SPIRE_VERSION="1.15.5"
TD="cc.test"
WORK="$(mktemp -d)"
PY="$(command -v python3)"
PIDS=()
ROOT_FILES=""
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
cleanup() {
  local pid
  {
    for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" || true; done
    pkill -f -- "$WORK/" || true
    for pid in ${PIDS[@]+"${PIDS[@]}"}; do wait "$pid" || true; done
    if [[ -n "$ROOT_FILES" ]]; then sudo -n rm -rf "$WORK" || true; fi
    rm -rf "$WORK" || true
  } 2>/dev/null
}
trap cleanup EXIT

m() { "$PY" "$LESSON/code/main.py" "$@"; }
MI="$WORK/mini"
SP="$WORK/spire"
mkdir -p "$WORK/dl" "$WORK/run" "$MI" "$SP"
LINUX=""
[[ "$(uname -s)" == "Linux" ]] && LINUX=1
SUDO=""
if [[ -n "$LINUX" ]] && sudo -n true 2>/dev/null; then SUDO=1; fi
command -v openssl >/dev/null || fail "openssl is required"

jfield() {
  "$PY" - "$@" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    value = value[int(key)] if isinstance(value, list) else value[key]
print(json.dumps(value) if isinstance(value, (dict, list)) else value)
PY
}
expect_exit() {
  local want="$1" out="$2" got=0
  shift 2
  "$@" > "$out" 2>&1 || got=$?
  [[ "$got" == "$want" ]] || fail "exit $got (want $want) from: $*
$(cat "$out")"
}
wait_for_socket() {
  local path="$1" what="$2" log="$3"
  for _ in $(seq 1 100); do [[ -S "$path" ]] && return 0; sleep 0.1; done
  fail "$what did not create $path: $(cat "$log" 2>/dev/null)"
}
wait_ok() {
  local what="$1" log="$2"
  shift 2
  for _ in $(seq 1 60); do "$@" > /dev/null 2>&1 && return 0; sleep 1; done
  fail "$what never succeeded: $(tail -30 "$log" 2>/dev/null)"
}
issue() {
  local dir="$1"
  shift
  m issue --out "$dir" "$@" --json > "$dir.json" 2>&1 || fail "miniid issue $*: $(cat "$dir.json")"
}
lifetime_of() { m inspect "$1" --json > "$1.inspect.json" || fail "miniid inspect $1 found problems: $(cat "$1.inspect.json")"; jfield "$1.inspect.json" certificates 0 lifetime_seconds; }
id_of() { m inspect "$1" --json > "$1.inspect.json" || fail "miniid inspect $1 found problems: $(cat "$1.inspect.json")"; jfield "$1.inspect.json" certificates 0 spiffe_id; }
uniform() { cp "$1/svid.0.pem" "$1/svid.pem"; cp "$1/svid.0.key" "$1/key.pem"; }

echo "▸ miniid id: the SPIFFE ID rules"
expect_exit 0 "$WORK/id-ok.txt" m id "spiffe://$TD/ns/shop/sa/web"
echo "  $(cat "$WORK/id-ok.txt")"
for bad in "spiffe://CC.test/web" "spiffe://$TD/web/" "spiffe://$TD/a/../b" "spiffe://$TD:8443/web" "spiffe://$TD/web?x=1" "https://$TD/web"; do
  expect_exit 1 "$WORK/id-bad.txt" m id "$bad"
done
echo "  six malformed IDs rejected, e.g. $(cat "$WORK/id-bad.txt")"

echo "▸ miniid CA and SVIDs, checked by openssl verify and openssl x509 -text"
m ca --trust-domain "$TD" --out "$MI/ca-cc" > "$WORK/ca-cc.txt" || fail "miniid ca: $(cat "$WORK/ca-cc.txt")"
m ca --trust-domain other.test --out "$MI/ca-other" > "$WORK/ca-other.txt" || fail "miniid ca: $(cat "$WORK/ca-other.txt")"
issue "$MI/api" --ca "$MI/ca-cc" --id "spiffe://$TD/api"
issue "$MI/issued-web" --ca "$MI/ca-cc" --id "spiffe://$TD/web" --ttl 300
issue "$MI/old-web" --ca "$MI/ca-cc" --id "spiffe://$TD/web" --ttl 60 --at "@$(( $(date +%s) - 7200 ))"
issue "$MI/other-web" --ca "$MI/ca-other" --id "spiffe://other.test/web"
issue "$MI/impostor" --ca "$MI/ca-other" --id "spiffe://$TD/web" --any-trust-domain
for purpose in sslclient sslserver; do
  openssl verify -CAfile "$MI/ca-cc/ca.pem" -purpose "$purpose" "$MI/issued-web/svid.pem" > "$WORK/ossl-$purpose.txt" 2>&1 || fail "openssl verify -purpose $purpose: $(cat "$WORK/ossl-$purpose.txt")"
  grep -q ": OK" "$WORK/ossl-$purpose.txt" || fail "openssl verify -purpose $purpose: $(cat "$WORK/ossl-$purpose.txt")"
done
openssl x509 -in "$MI/issued-web/svid.pem" -noout -text > "$WORK/web-x509.txt"
for words in "URI:spiffe://$TD/web" "CA:FALSE" "Digital Signature" "TLS Web Server Authentication" "TLS Web Client Authentication" "ecdsa-with-SHA256"; do
  grep -qF "$words" "$WORK/web-x509.txt" || fail "openssl x509 -text has no '$words': $(cat "$WORK/web-x509.txt")"
done
grep -qE "prime256v1|P-256" "$WORK/web-x509.txt" || fail "the key is not on P-256: $(cat "$WORK/web-x509.txt")"
openssl x509 -in "$MI/ca-cc/ca.pem" -noout -text > "$WORK/ca-x509.txt"
for words in "CA:TRUE" "Certificate Sign" "URI:spiffe://$TD"; do
  grep -qF "$words" "$WORK/ca-x509.txt" || fail "openssl x509 -text of the CA has no '$words': $(cat "$WORK/ca-x509.txt")"
done
openssl x509 -in "$MI/issued-web/svid.pem" -noout -pubkey > "$WORK/cert-pub.pem"
openssl pkey -in "$MI/issued-web/key.pem" -pubout > "$WORK/key-pub.pem" 2> "$WORK/pkey.err" || fail "openssl cannot read miniid's PKCS#8 key: $(cat "$WORK/pkey.err")"
cmp -s "$WORK/cert-pub.pem" "$WORK/key-pub.pem" || fail "the private key does not match the certificate"
echo "  openssl: chain OK for sslclient and sslserver, URI SAN, CA:FALSE, digitalSignature, both EKUs, P-256, key matches"
GOT=0
openssl verify -CAfile "$MI/ca-cc/ca.pem" "$MI/old-web/svid.pem" > "$WORK/ossl-old.txt" 2>&1 || GOT=$?
[[ "$GOT" != "0" ]] && grep -q "error 10 " "$WORK/ossl-old.txt" || fail "an SVID issued two hours ago with a 60 s TTL should fail with error 10: $(cat "$WORK/ossl-old.txt")"
cat "$MI/ca-cc/ca.pem" "$MI/ca-other/ca.pem" > "$WORK/both-cas.pem"
openssl verify -CAfile "$WORK/both-cas.pem" "$MI/impostor/svid.pem" > "$WORK/ossl-impostor.txt" 2>&1 || fail "with both CAs in one flat store openssl accepts the impostor; got: $(cat "$WORK/ossl-impostor.txt")"
expect_exit 1 "$WORK/verify-impostor.json" m verify "$MI/impostor/svid.pem" --bundle "$MI/ca-cc/bundle.pem" --bundle "$MI/ca-other/bundle.json" --json
[[ "$(jfield "$WORK/verify-impostor.json" reason)" == "unknown-authority" ]] || fail "miniid should reject the impostor per trust domain: $(cat "$WORK/verify-impostor.json")"
echo "  impostor spiffe://$TD/web signed by other.test: openssl with a flat store says OK, miniid says: $(jfield "$WORK/verify-impostor.json" detail)"

echo "▸ miniid agent: a Workload API on a Unix socket that attests callers"
UID_NOW="$(id -u)"
cat > "$WORK/entries.json" <<EOF
[{"spiffe_id": "spiffe://$TD/web", "selectors": ["unix:uid:$UID_NOW"], "ttl": 300},
 {"spiffe_id": "spiffe://$TD/batch", "selectors": ["unix:uid:0"], "ttl": 300}]
EOF
MSOCK="$WORK/run/miniid.sock"
ATTEST=()
[[ -n "$LINUX" ]] || ATTEST=(--static-selectors "unix:uid:$UID_NOW")
m agent --socket "$MSOCK" --ca "$MI/ca-cc" --entries "$WORK/entries.json" --federate "$MI/ca-other/bundle.pem" --log "$WORK/run/miniid-agent.log" ${ATTEST[@]+"${ATTEST[@]}"} > "$WORK/run/miniid-agent.out" 2>&1 &
PIDS+=("$!")
wait_for_socket "$MSOCK" "the miniid agent" "$WORK/run/miniid-agent.out"
m fetch --socket "$MSOCK" --out "$MI/web" --json > "$MI/web-fetch.json" 2>&1 || fail "miniid fetch: $(cat "$MI/web-fetch.json") $(cat "$WORK/run/miniid-agent.log")"
uniform "$MI/web"
[[ "$(id_of "$MI/web/svid.pem")" == "spiffe://$TD/web" ]] || fail "uid $UID_NOW should get spiffe://$TD/web: $(cat "$MI/web-fetch.json")"
[[ -s "$MI/web/federated_bundle.0.0.pem" ]] || fail "the agent should hand out the federated other.test bundle"
MINI_WEB_LIFE="$(lifetime_of "$MI/web/svid.pem")"
if [[ -n "$LINUX" ]]; then
  grep -q "\"unix:uid:$UID_NOW\"" "$WORK/run/miniid-agent.log" || fail "the agent log should show selectors from SO_PEERCRED and /proc: $(cat "$WORK/run/miniid-agent.log")"
fi
if [[ -n "$SUDO" ]]; then
  ROOT_FILES=1
  sudo -n "$PY" "$LESSON/code/main.py" fetch --socket "$MSOCK" --out "$MI/batch" --json > "$MI/batch-fetch.json" 2>&1 || fail "miniid fetch as root: $(cat "$MI/batch-fetch.json")"
  sudo -n chown -R "$(id -u):$(id -g)" "$MI/batch"
  uniform "$MI/batch"
  [[ "$(id_of "$MI/batch/svid.pem")" == "spiffe://$TD/batch" ]] || fail "root should get spiffe://$TD/batch: $(cat "$MI/batch-fetch.json")"
  grep -q '"unix:uid:0"' "$WORK/run/miniid-agent.log" || fail "the agent log should show uid 0 for the root caller: $(cat "$WORK/run/miniid-agent.log")"
  echo "  same socket: uid $UID_NOW gets spiffe://$TD/web, root gets spiffe://$TD/batch (SO_PEERCRED)"
else
  echo "  (no Linux or no passwordless sudo; batch is issued directly instead of attested as root)"
  issue "$MI/batch" --ca "$MI/ca-cc" --id "spiffe://$TD/batch" --ttl 300
fi
echo "  agent log: $(head -1 "$WORK/run/miniid-agent.log")"

SPIRE=""
if [[ -n "$LINUX" ]]; then
  SPIRE=1
  case "$(uname -m)" in
    x86_64) ARCH=amd64 ;;
    aarch64) ARCH=arm64 ;;
    *) fail "no SPIRE release for $(uname -m)" ;;
  esac
  TARBALL="spire-$SPIRE_VERSION-linux-$ARCH-musl.tar.gz"
  BASE="https://github.com/spiffe/spire/releases/download/v$SPIRE_VERSION"
  echo "▸ SPIRE $SPIRE_VERSION: download the release and check it against the published sha256"
  curl -fsSL --retry 3 -o "$WORK/dl/$TARBALL" "$BASE/$TARBALL" || fail "download $BASE/$TARBALL"
  curl -fsSL --retry 3 -o "$WORK/dl/sha256sum.txt" "$BASE/spire-$SPIRE_VERSION-linux-$ARCH-musl_sha256sum.txt" || fail "download the checksum file"
  WANT="$(awk -v f="$TARBALL" '$2 == f {print $1}' "$WORK/dl/sha256sum.txt")"
  GOT="$(sha256sum "$WORK/dl/$TARBALL" | awk '{print $1}')"
  [[ -n "$WANT" && "$GOT" == "$WANT" ]] || fail "checksum mismatch for $TARBALL: got $GOT, published: $(cat "$WORK/dl/sha256sum.txt")"
  tar -xzf "$WORK/dl/$TARBALL" -C "$WORK/dl"
  BIN="$WORK/dl/spire-$SPIRE_VERSION/bin"
  echo "  sha256 $GOT matches"

  echo "▸ SPIRE server and agent on the runner, joined with a token"
  PORT="$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
  SSOCK="$SP/server/api.sock"
  ASOCK="$SP/agent/api.sock"
  mkdir -p "$SP/server/data" "$SP/agent/data"
  cat > "$SP/server.conf" <<EOF
server {
    bind_address = "127.0.0.1"
    bind_port = "$PORT"
    socket_path = "$SSOCK"
    trust_domain = "$TD"
    data_dir = "$SP/server/data"
    log_level = "INFO"
    ca_key_type = "ec-p256"
    ca_ttl = "24h"
    default_x509_svid_ttl = "1h"
}
plugins {
    DataStore "sql" {
        plugin_data {
            database_type = "sqlite3"
            connection_string = "$SP/server/data/datastore.sqlite3"
        }
    }
    KeyManager "memory" {
        plugin_data {}
    }
    NodeAttestor "join_token" {
        plugin_data {}
    }
}
EOF
  cat > "$SP/agent.conf" <<EOF
agent {
    data_dir = "$SP/agent/data"
    log_level = "INFO"
    trust_domain = "$TD"
    server_address = "127.0.0.1"
    server_port = $PORT
    socket_path = "$ASOCK"
    insecure_bootstrap = true
}
plugins {
    KeyManager "memory" {
        plugin_data {}
    }
    NodeAttestor "join_token" {
        plugin_data {}
    }
    WorkloadAttestor "unix" {
        plugin_data {}
    }
}
EOF
  "$BIN/spire-server" run -config "$SP/server.conf" > "$SP/server.log" 2>&1 &
  PIDS+=("$!")
  wait_ok "spire-server healthcheck" "$SP/server.log" "$BIN/spire-server" healthcheck -socketPath "$SSOCK"
  "$BIN/spire-server" token generate -socketPath "$SSOCK" -ttl 600 > "$SP/token.txt" 2>&1 || fail "token generate: $(cat "$SP/token.txt")"
  TOKEN="$(awk '/^Token:/ {print $2}' "$SP/token.txt")"
  [[ -n "$TOKEN" ]] || fail "no token in: $(cat "$SP/token.txt")"
  "$BIN/spire-agent" run -config "$SP/agent.conf" -joinToken "$TOKEN" > "$SP/agent.log" 2>&1 &
  PIDS+=("$!")
  wait_ok "spire-agent healthcheck" "$SP/agent.log" "$BIN/spire-agent" healthcheck -socketPath "$ASOCK"
  AGENT_ID="spiffe://$TD/spire/agent/join_token/$TOKEN"
  for _ in $(seq 1 30); do
    "$BIN/spire-server" agent list -socketPath "$SSOCK" > "$SP/agents.txt" 2>&1 || true
    grep -qF "$AGENT_ID" "$SP/agents.txt" && break
    sleep 1
  done
  grep -qF "$AGENT_ID" "$SP/agents.txt" || fail "the attested agent should be $AGENT_ID: $(cat "$SP/agents.txt") $(tail -20 "$SP/agent.log")"
  echo "  agent attested as $AGENT_ID"

  echo "▸ SPIRE: two registration entries keyed by UID, fetched through the Workload API"
  "$BIN/spire-server" entry create -socketPath "$SSOCK" -parentID "$AGENT_ID" -spiffeID "spiffe://$TD/web" -selector "unix:uid:$UID_NOW" -x509SVIDTTL 300 > "$SP/entry-web.txt" 2>&1 || fail "entry create web: $(cat "$SP/entry-web.txt")"
  "$BIN/spire-server" entry create -socketPath "$SSOCK" -parentID "$AGENT_ID" -spiffeID "spiffe://$TD/batch" -selector "unix:uid:0" -x509SVIDTTL 300 > "$SP/entry-batch.txt" 2>&1 || fail "entry create batch: $(cat "$SP/entry-batch.txt")"
  fetch_spire() {
    local dir="$1"
    shift
    mkdir -p "$dir"
    for _ in $(seq 1 30); do
      if "$@" "$BIN/spire-agent" api fetch x509 -socketPath "$ASOCK" -write "$dir" > "$dir.out" 2>&1; then return 0; fi
      sleep 2
    done
    fail "spire-agent api fetch x509 never issued an SVID: $(cat "$dir.out") $(tail -20 "$SP/agent.log")"
  }
  fetch_spire "$SP/web"
  uniform "$SP/web"
  SPIRE_WEB_ID="$(id_of "$SP/web/svid.pem")"
  SPIRE_WEB_LIFE="$(lifetime_of "$SP/web/svid.pem")"
  [[ "$SPIRE_WEB_ID" == "spiffe://$TD/web" ]] || fail "SPIRE gave uid $UID_NOW $SPIRE_WEB_ID"
  [[ "$(jfield "$SP/web/svid.pem.inspect.json" certificates 0 trust_domain)" == "$TD" ]] || fail "trust domain: $(cat "$SP/web/svid.pem.inspect.json")"
  (( SPIRE_WEB_LIFE >= 300 && SPIRE_WEB_LIFE <= 315 )) || fail "SPIRE's SVID lifetime should be the 300 s TTL plus the backdate, got $SPIRE_WEB_LIFE"
  (( MINI_WEB_LIFE == SPIRE_WEB_LIFE || MINI_WEB_LIFE - SPIRE_WEB_LIFE <= 2 && SPIRE_WEB_LIFE - MINI_WEB_LIFE <= 2 )) || fail "lifetimes differ: miniid $MINI_WEB_LIFE s, SPIRE $SPIRE_WEB_LIFE s"
  echo "  uid $UID_NOW: SPIRE $SPIRE_WEB_ID ($SPIRE_WEB_LIFE s), miniid $(id_of "$MI/web/svid.pem") ($MINI_WEB_LIFE s); SPIRE's SVID passes miniid's X509-SVID checks"
  if [[ -n "$SUDO" ]]; then
    fetch_spire "$SP/batch" sudo -n
    sudo -n chown -R "$(id -u):$(id -g)" "$SP/batch"
    uniform "$SP/batch"
    [[ "$(id_of "$SP/batch/svid.pem")" == "spiffe://$TD/batch" ]] || fail "SPIRE should give root spiffe://$TD/batch, got $(id_of "$SP/batch/svid.pem")"
    echo "  root: SPIRE spiffe://$TD/batch, miniid $(id_of "$MI/batch/svid.pem")"
  else
    echo "  (no passwordless sudo; SPIRE's batch identity is minted instead of attested as root)"
    mkdir -p "$SP/batch"
    "$BIN/spire-server" x509 mint -socketPath "$SSOCK" -spiffeID "spiffe://$TD/batch" -ttl 1h -write "$SP/batch" > "$SP/batch.out" 2>&1 || fail "x509 mint batch: $(cat "$SP/batch.out")"
  fi
  openssl verify -CAfile "$SP/web/bundle.0.pem" -purpose sslclient "$SP/web/svid.pem" > "$SP/ossl.txt" 2>&1 || fail "openssl rejects SPIRE's SVID: $(cat "$SP/ossl.txt")"

  echo "▸ SPIRE's SPIFFE bundle, read by miniid"
  "$BIN/spire-server" bundle show -socketPath "$SSOCK" -format spiffe > "$SP/bundle.json" 2> "$SP/bundle.err" || fail "bundle show: $(cat "$SP/bundle.err")"
  m inspect "$SP/bundle.json" --trust-domain "$TD" --json > "$SP/bundle-inspect.json" || fail "miniid cannot read SPIRE's bundle: $(cat "$SP/bundle-inspect.json")"
  m inspect "$SP/web/bundle.0.pem" --json > "$SP/bundle-pem-inspect.json" || fail "miniid on bundle.0.pem: $(cat "$SP/bundle-pem-inspect.json")"
  "$PY" - "$SP/bundle-inspect.json" "$SP/bundle-pem-inspect.json" <<'PY' || fail "the SPIFFE bundle and the agent's bundle.0.pem should hold the same CA"
import json, sys
spiffe, pem = (json.load(open(path)) for path in sys.argv[1:3])
left = {c["sha256"] for c in spiffe["certificates"]}
right = {c["sha256"] for c in pem["certificates"]}
roles = {c["role"] for c in spiffe["certificates"]}
if left != right or roles != {"ca"}:
    sys.exit(f"spiffe bundle {left} roles {roles}, bundle.0.pem {right}")
print(f"  {spiffe['kind']}: {len(left)} x509-svid authority, the same as bundle.0.pem")
PY
  expect_exit 0 "$SP/verify-web.txt" m verify "$SP/web/svid.pem" --bundle "$TD=$SP/bundle.json" --allow "spiffe://$TD/web"
  echo "  $(cat "$SP/verify-web.txt")"
  mkdir -p "$SP/api"
  "$BIN/spire-server" x509 mint -socketPath "$SSOCK" -spiffeID "spiffe://$TD/api" -ttl 1h -write "$SP/api" > "$SP/api.out" 2>&1 || fail "x509 mint api: $(cat "$SP/api.out")"
  [[ "$(id_of "$SP/api/svid.pem")" == "spiffe://$TD/api" ]] || fail "minted api SVID: $(cat "$SP/api.out")"
  cp "$SP/web/bundle.0.pem" "$SP/bundle-cc.pem"
else
  echo "  (not Linux: the SPIRE half is skipped; it runs on the CI runner)"
fi
cp "$MI/ca-cc/bundle.pem" "$MI/bundle-cc.pem"

cat > "$WORK/matrix.py" <<'PY'
import json, os, subprocess, sys, threading, time

lesson, issuer, home, api, web, batch, short, foreign, impostor, cc_bundle, other_bundle, out_path = sys.argv[1:13]
sys.path.insert(0, os.path.join(lesson, "code"))
import main

PY = sys.executable
MAIN = os.path.join(lesson, "code", "main.py")
TD = "cc.test"
os.makedirs(home, exist_ok=True)
servers = []


def start(name, svid_dir, bundles, allows, extra=()):
    port_file, log, err = (os.path.join(home, name + suffix) for suffix in (".port", ".jsonl", ".err"))
    args = [PY, MAIN, "serve", "--svid", os.path.join(svid_dir, "svid.pem"), "--key", os.path.join(svid_dir, "key.pem"), "--listen", "127.0.0.1:0", "--port-file", port_file, "--decisions", log]
    args += [part for bundle in bundles for part in ("--bundle", bundle)] + [part for allow in allows for part in ("--allow", allow)] + list(extra)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=open(err, "w"))
    server = {"name": name, "proc": proc, "port_file": port_file, "log": log, "err": err}
    servers.append(server)
    return server


def ready(server):
    deadline = time.time() + 30
    while time.time() < deadline:
        if server["proc"].poll() is not None:
            sys.exit(f"{issuer}: server {server['name']} exited: {open(server['err']).read()}")
        if os.path.exists(server["port_file"]) and open(server["port_file"]).read().endswith("\n"):
            server["port"] = int(open(server["port_file"]).read())
            return
        time.sleep(0.05)
    sys.exit(f"{issuer}: server {server['name']} did not start: {open(server['err']).read()}")


def decisions(server):
    if not os.path.exists(server["log"]):
        return []
    return [json.loads(line) for line in open(server["log"]) if line.strip()]


def call(server, client, *extra):
    args = [PY, MAIN, "call", f"127.0.0.1:{server['port']}", "--svid", os.path.join(client, "svid.pem"), "--key", os.path.join(client, "key.pem"),
            "--bundle", f"{TD}={cc_bundle}", "--bundle", f"other.test={other_bundle}", "--expect-server", f"spiffe://{TD}/api", "--timeout", "10", "--json", *extra]
    done = subprocess.run(args, capture_output=True, text=True, timeout=120)
    try:
        return json.loads(done.stdout)
    except ValueError:
        sys.exit(f"{issuer}: miniid call {' '.join(args[4:])} printed no JSON: {done.stdout} {done.stderr}")


def row(label, server, client, want_decision, want_reason):
    before = len(decisions(server))
    result = call(server, client)
    deadline = time.time() + 10
    while len(decisions(server)) <= before and time.time() < deadline:
        time.sleep(0.05)
    logged = decisions(server)[before] if len(decisions(server)) > before else {"decision": "none", "stage": "none", "reason": "none"}
    precise = logged["stage"] == "handshake" and logged["reason"] not in ("no-certificate", "handshake-failed", "connection-closed")
    reason = logged["reason"] if precise else result["reason"]
    return {"label": label, "decision": result["decision"], "reason": reason, "want": [want_decision, want_reason],
            "client": [result["decision"], result["stage"], result["reason"]], "server": [logged["decision"], logged["stage"], logged["reason"]], "detail": result["detail"]}


try:
    strict = start("strict", api, [f"{TD}={cc_bundle}"], [f"spiffe://{TD}/web"])
    federated = start("federated", api, [f"{TD}={cc_bundle}", f"other.test={other_bundle}"], [f"spiffe://{TD}/web", "spiffe://other.test/web"])
    rogue = start("rogue", batch, [f"{TD}={cc_bundle}"], [f"spiffe://{TD}/web"])
    longlived = start("longlived", api, [f"{TD}={cc_bundle}"], [f"spiffe://{TD}/web"])
    enforcing = start("enforcing", api, [f"{TD}={cc_bundle}"], [f"spiffe://{TD}/web"], ["--enforce-expiry"])
    for server in servers:
        ready(server)
    not_after = main.load_certificates(open(os.path.join(short, "svid.pem")).read(), short)[0].not_after.timestamp()
    left = not_after - time.time()
    if left < 3:
        sys.exit(f"{issuer}: the short-lived SVID has only {left:.1f} s left before the first request; start faster or raise its TTL")
    interval = f"{left + 2:.1f}"
    mid = {}
    threads = [threading.Thread(target=lambda name=name, server=server: mid.update({name: call(server, short, "--count", "2", "--interval", interval, "--message", "tick")})) for name, server in (("longlived", longlived), ("enforcing", enforcing))]
    for thread in threads:
        thread.start()
    rows = [
        row("right-id", strict, web, "accept", "ok"),
        row("wrong-id", strict, batch, "reject", "not-allowed"),
        row("foreign-ca", strict, foreign, "reject", "unknown-authority"),
        row("impostor", federated, impostor, "reject", "unknown-authority"),
        row("federated-peer", federated, foreign, "accept", "ok"),
        row("wrong-server", rogue, web, "reject", "not-allowed"),
    ]
    for thread in threads:
        thread.join(120)
    rows.insert(2, row("expired", strict, short, "reject", "expired"))
finally:
    for server in servers:
        server["proc"].terminate()
    for server in servers:
        try:
            server["proc"].wait(10)
        except subprocess.TimeoutExpired:
            server["proc"].kill()

problems = []
for item in rows:
    print(f"  {issuer:6} {item['label']:15} {item['decision']:6} {item['reason']:18} client={'/'.join(item['client'])} server={'/'.join(item['server'])}")
    if [item["decision"], item["reason"]] != item["want"]:
        problems.append(f"{item['label']}: got {item['decision']}/{item['reason']}, want {'/'.join(item['want'])}: {item['detail']}")
long_result, enforced = mid.get("longlived", {}), mid.get("enforcing", {})
echoes = [reply.get("echo") for reply in long_result.get("replies", [])[1:]]
print(f"  {issuer:6} mid-connection  certificate expired between two requests: plain TLS {long_result.get('decision')} {echoes}, --enforce-expiry {enforced.get('decision')}/{enforced.get('reason')}")
if long_result.get("decision") != "accept" or echoes != ["tick", "tick"]:
    problems.append(f"the open connection should keep working after the certificate expired: {long_result}")
if (enforced.get("decision"), enforced.get("stage"), enforced.get("reason")) != ("reject", "denied", "expired"):
    problems.append(f"--enforce-expiry should close the connection with reason expired: {enforced}")
json.dump({"issuer": issuer, "rows": [[item["label"], item["decision"], item["reason"]] for item in rows], "mid": [long_result.get("decision"), echoes, enforced.get("decision"), enforced.get("reason")]}, open(out_path, "w"), indent=2)
if problems:
    sys.exit(f"{issuer}: " + "\n".join(problems))
PY

echo "▸ two mTLS services, with miniid-issued and with SPIRE-issued SVIDs: the same accept and reject decisions"
DRIVERS=()
issue "$MI/short" --ca "$MI/ca-cc" --id "spiffe://$TD/web" --ttl 15
"$PY" "$WORK/matrix.py" "$LESSON" miniid "$WORK/run/m-miniid" "$MI/api" "$MI/web" "$MI/batch" "$MI/short" "$MI/other-web" "$MI/impostor" "$MI/bundle-cc.pem" "$MI/ca-other/bundle.pem" "$WORK/matrix-miniid.json" > "$WORK/matrix-miniid.out" 2>&1 &
DRIVERS+=("$!")
if [[ -n "$SPIRE" ]]; then
  mkdir -p "$SP/short"
  "$BIN/spire-server" x509 mint -socketPath "$SSOCK" -spiffeID "spiffe://$TD/web" -ttl 15s -write "$SP/short" > "$SP/short.out" 2>&1 || fail "x509 mint short: $(cat "$SP/short.out")"
  "$PY" "$WORK/matrix.py" "$LESSON" spire "$WORK/run/m-spire" "$SP/api" "$SP/web" "$SP/batch" "$SP/short" "$MI/other-web" "$MI/impostor" "$SP/bundle-cc.pem" "$MI/ca-other/bundle.pem" "$WORK/matrix-spire.json" > "$WORK/matrix-spire.out" 2>&1 &
  DRIVERS+=("$!")
fi
FAILED=""
for pid in "${DRIVERS[@]}"; do wait "$pid" || FAILED=1; done
cat "$WORK/matrix-miniid.out"
[[ -n "$SPIRE" ]] && cat "$WORK/matrix-spire.out"
[[ -z "$FAILED" ]] || fail "a decision matrix did not match the expected decisions (see above)"
if [[ -n "$SPIRE" ]]; then
  "$PY" - "$WORK/matrix-miniid.json" "$WORK/matrix-spire.json" <<'PY' || fail "SPIRE-issued and miniid-issued SVIDs led to different decisions"
import json, sys
mini, spire = (json.load(open(path)) for path in sys.argv[1:3])
if mini["rows"] != spire["rows"] or mini["mid"] != spire["mid"]:
    sys.exit(f"miniid {mini}\nspire  {spire}")
print(f"  identical: {len(mini['rows'])} decisions and the mid-connection expiry, for both issuers")
PY
fi

echo "SMOKE OK: security/workload-identity"
