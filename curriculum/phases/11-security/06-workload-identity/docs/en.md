# Zero Trust Is Mutual TLS With Short-Lived Identities

*A workload proves who it is with a certificate issued minutes ago, and the peer checks its SPIFFE ID, not the network it came from.*

## Metadata

| Field | Value |
|---|---|
| Type | Build |
| Languages | Python |
| Prerequisites | [TLS Is a Handshake and a Chain You Can Verify](https://cloudcaptain.io/learn/networking/tls-handshake) |
| Time | 120 minutes |
| Tools | spire-server, spire-agent, openssl, python3 |

<Quiz stage="pre" />

## The Problem

Tuesday, 03:20. The fraud team notices refunds being issued to accounts that never asked for one. The `payments-api` logs show every request as legitimate: the source address is inside `10.20.0.0/16`, the cluster's pod range that the network policy allows, and the `X-Internal-Key` header carries the shared key six internal services use. The address belongs to a node that runs 40 pods. Nobody can say which workload made the calls. It takes a day to find that a `report-batch` pod pulled a compromised dependency, read the key from its own environment (all six services get it from the same Secret) and called the refund endpoint directly. Rotating the key means a coordinated redeploy of six teams, so the last rotation was fourteen months ago.

The previous attempt to fix this had stalled. A platform engineer had issued each service a certificate from an internal CA and turned on mutual TLS. The certificates were valid for two years, sat in Kubernetes Secrets that were copied between three clusters, and the servers accepted any certificate the CA had signed, so `report-batch` could still call `payments-api`; it now did so over an encrypted channel. Then one of those certificates expired on a Saturday and took checkout down until someone found the renewal runbook.

Zero trust says three concrete things here. Every workload gets an identity that names the workload (`spiffe://prod.example/ns/payments/sa/api`), not its address. The identity is issued only after something other than the workload vouches for it: the kernel reports which user and process is asking, or the kubelet reports the pod and its ServiceAccount, so no secret has to be shipped to bootstrap it. And the identity is short-lived and rotated automatically, so a stolen one is useless within the hour and an expired one is never a manual task. Every connection is mutual TLS, and the server checks the caller's identity against a list before it reads a byte of the request. You are going to build `miniid`, which issues these identities from a CA you write, hands them out over a Unix socket to callers it has attested, and runs two services that accept and reject each other by ID. Then you will run SPIRE, the reference implementation, and see the same decisions.

## The Concept

Think of a building with badge readers. A visitor does not print their own badge. The front desk checks who they are with a source the visitor does not control (the HR system, an ID card), prints a badge with their name that works for one hour, and prints a new one before it runs out. Every door checks two things: that the badge carries the hologram of this building's badge office, and that the name on it is on the door's list. A partner company's badges open your doors only where you have put the partner's hologram on file, and a partner's badge office cannot print badges with your company's names on them.

**SPIFFE** (Secure Production Identity Framework For Everyone) defines those pieces as open specifications:

- A **SPIFFE ID** is a URI, `spiffe://trust-domain/path`. The **trust domain** (`cc.test`, `prod.example`) is the badge office: one root of trust, one set of signing keys. The path names a workload inside it, often after the platform's own identity (`/ns/payments/sa/api` for a Kubernetes ServiceAccount). The ID says nothing about where the workload runs.
- An **SVID** (SPIFFE Verifiable Identity Document) is the badge. The X.509 form is an ordinary certificate with exactly one URI SAN, the SPIFFE ID, plus `CA:FALSE`, `digitalSignature`, and `serverAuth` and `clientAuth`, so the same certificate works on both ends of a connection. Its lifetime is typically one hour.
- A **trust bundle** is the set of CA certificates of one trust domain. Validation is per trust domain: the trust domain in the peer's SPIFFE ID selects the bundle, and only CAs in that bundle may have signed it. Federation means holding the bundles of other trust domains, each used only for IDs in that domain.
- The **Workload API** is how a workload gets its SVID: it connects to a Unix socket served by a local **agent**. The agent asks the kernel who is on the other end of the socket (`SO_PEERCRED` returns the caller's PID, UID and GID), turns what it learns into **selectors** (`unix:uid:1001`, `k8s:sa:api`), finds the **registration entries** whose selectors all match, and returns an SVID for each, with its private key and the bundles. The workload proves nothing and carries no secret; the platform vouches for it.
- **Rotation** happens long before expiry. The agent issues a fresh SVID when half of the lifetime has passed and pushes it on the open stream, so a workload that keeps the stream open never holds an expired certificate.

```mermaid
flowchart LR
  W["workload process, uid 1001"] -->|"connect to the Unix socket"| A["agent: Workload API"]
  A -->|"SO_PEERCRED: pid, uid, gid"| K["kernel and /proc"]
  A -->|"selectors unix:uid:1001"| E["registration entries"]
  E -->|"spiffe://cc.test/web, TTL 1h"| S["CA signs the X509-SVID"]
  S -->|"SVID, key, bundles; again at half-life"| W
  W -->|"mutual TLS"| P["peer: spiffe://cc.test/api"]
  P -->|"1. chain to the cc.test bundle"| B["trust bundle per trust domain"]
  P -->|"2. is spiffe://cc.test/web allowed?"| Z["authorization policy"]
```

Mutual TLS is the transport, and it is not enough on its own. The TLS library checks the chain, the dates and the key usages, so after the handshake you know the peer holds a key for a certificate your CAs signed. Whether that peer may call you is a separate decision you make on the SPIFFE ID, and skipping it is exactly the stalled rollout from the problem: every workload in the trust domain could call every other one.

One more property surprises people. TLS checks certificates **during the handshake only**. A connection established at 10:59 with a certificate that expires at 11:00 keeps working at 11:30; TLS 1.3 has no renegotiation that would look again. Short lifetimes bound how long a stolen key can open *new* connections, and the next reconnect is where an expired certificate fails. If long-lived connections matter, cap the connection age as well.

## Build It

`code/main.py` is `miniid`, standard library only, with nine subcommands: `id` checks a SPIFFE ID, `ca` creates a trust domain's CA, `issue` signs an X509-SVID, `inspect` prints certificates and bundles and checks them against the X509-SVID rules, `verify` validates an SVID per trust domain, `serve` and `call` are two services that talk mutual TLS with Python's `ssl` and authorise each other by ID, `agent` serves a Workload API on a Unix socket that attests callers with `SO_PEERCRED`, and `fetch` is its client. Exit codes are 0 for accepted, 1 for a rejection or an invalid ID, 2 for bad input.

### Step 1: a SPIFFE ID is a URI with strict rules

The specification narrows RFC 3986 to make IDs comparable as strings: a lowercase scheme, a trust domain of lowercase letters, digits, `.`, `-` and `_`, no port or userinfo, no query, fragment or percent-encoding, and path segments that are never empty, `.` or `..` and never end with `/`:

```python
authority, slash, rest = text[len(SCHEME):].partition("/")
if not authority:
    raise SpiffeIdError(f"{text!r} has an empty trust domain")
if "@" in authority or ":" in authority:
    raise SpiffeIdError(f"{authority!r} carries userinfo or a port; a trust domain is a bare name")
check_trust_domain(authority)
path = slash + rest
if path.endswith("/"):
    raise SpiffeIdError(f"{text!r} ends with '/'")
for segment in path.split("/")[1:]:
    if segment in ("", ".", ".."):
        raise SpiffeIdError(f"{text!r} has an empty, '.' or '..' path segment")
```

Because of these rules `spiffe://cc.test/web` and `spiffe://cc.test/./web` cannot both exist, so an authorization list can compare strings. `spiffe://cc.test` with no path is valid too: it is the ID of the trust domain itself, which the CA carries and a workload never may. IDs are limited to 2048 bytes.

### Step 2: DER by hand

The prerequisite lesson read certificates. This one writes them, so `miniid` needs the other half of DER: tag, length (one byte below 128, otherwise `0x80 | n` and `n` length bytes), value. Two details trip up hand-written encoders. INTEGERs are two's complement, so 128 needs a leading zero byte. And a named BIT STRING such as `keyUsage` must drop its trailing zero bits and say how many it dropped in the first content byte:

```python
def der_bits(positions: Iterable[int]) -> bytes:
    ordered = sorted(set(positions))
    if not ordered:
        return der(0x03, b"\x00")
    raw = bytearray(ordered[-1] // 8 + 1)
    for position in ordered:
        raw[position // 8] |= 0x80 >> (position % 8)
    return der(0x03, bytes([7 - ordered[-1] % 8]), bytes(raw))
```

`digitalSignature` is bit 0, so a leaf's keyUsage is `03 02 07 80`; a CA's `keyCertSign` and `cRLSign` (bits 5 and 6) give `03 02 01 06`. Encoding the leaf's as `03 02 00 80` is the same set of flags but not DER, and strict verifiers reject the certificate.

### Step 3: ECDSA P-256 with pow() and a deterministic nonce

Signing needs a fresh secret `k` for every signature; reuse one or let it be guessable and the private key falls out of two signatures with high-school algebra. RFC 6979 derives `k` from the key and the message hash with HMAC, so no random number generator is involved at signing time and the result can be checked against the RFC's published vectors:

```python
def ecdsa_sign(key: EcKey, digest: bytes) -> Tuple[int, int]:
    curve = key.curve
    z = bits_to_int(digest, curve.n)
    for k in rfc6979_nonces(curve, key.d, digest):
        point = scalar_mult(curve, k, curve.generator)
        assert point is not None
        r = point[0] % curve.n
        s = pow(k, -1, curve.n) * (z + r * key.d) % curve.n
        if r and s:
            return r, s
    raise AssertionError("the nonce stream never ends")
```

The tests check the public key and both P-256 SHA-256 vectors (`sample` and `test`) of RFC 6979 appendix A.2.5, nonce and signature. The signature goes into the certificate as `SEQUENCE { INTEGER r, INTEGER s }` inside a BIT STRING. Key generation is `secrets.randbelow(n - 1) + 1` and one scalar multiplication. This arithmetic is not constant time: it is for reading, not for production keys.

### Step 4: what makes a certificate an X509-SVID

A CA certificate gets `CA:TRUE`, `keyCertSign` and `cRLSign`, and the trust domain's own ID as its URI SAN. A leaf gets the extensions the X509-SVID specification asks for:

```python
extensions = [
    der_extension(OID_BASIC_CONSTRAINTS, der(0x30), critical=True),
    der_extension(OID_KEY_USAGE, der_bits([0]), critical=True),
    der_extension(OID_EXT_KEY_USAGE, der(0x30, der_oid(OID_SERVER_AUTH), der_oid(OID_CLIENT_AUTH))),
    der_extension(OID_SAN, san),
    der_extension(OID_SKI, der(0x04, key_id(key))),
    der_extension(OID_AKI, der(0x30, der(0x80, authority.cert.subject_key_id))),
]
```

An empty SEQUENCE is `basicConstraints` with `cA` left at its default, false. The SAN holds the SPIFFE ID as a URI (`[6]`, tag `0x86`) and optionally DNS names for clients that still check hostnames. The authority key identifier links the leaf to the CA's subject key identifier, a SHA-1 of the CA's public key. `issue_svid()` refuses IDs outside the CA's trust domain (`--any-trust-domain` lifts that so you can build the attack in Step 6), starts the validity 10 seconds in the past so a peer with a slightly slow clock does not reject a brand-new certificate (SPIRE uses the same 10-second cushion), and caps the end at the CA's own expiry. `svid_problems()` checks any certificate against the specification's rules, which is how `inspect` reviews what SPIRE issues.

### Step 5: keys and bundles on disk

The private key is written as PKCS#8 (`-----BEGIN PRIVATE KEY-----`), an algorithm identifier for EC P-256 wrapped around an RFC 5915 `ECPrivateKey`, the same format SPIRE's Workload API returns and Python's `ssl` and OpenSSL load. The trust bundle exists in two forms: PEM, a list of CA certificates, and the SPIFFE bundle format, a JWK Set in which every CA is an entry with `"use": "x509-svid"` and the DER certificate in `x5c`, plus a `spiffe_sequence` that increases with every change. `parse_bundle()` reads both, ignores the `jwt-svid` keys that SPIRE publishes in the same document, and takes the trust domain from the CA's URI SAN when the caller does not name it:

```bash
python3 code/main.py ca --trust-domain cc.test --out ca-cc       # ca.pem, ca-key.pem (0600), bundle.pem, bundle.json
python3 code/main.py inspect ca-cc/bundle.json                   # SPIFFE bundle for cc.test (sequence 1), 1 certificate(s)
```

### Step 6: validation picks the bundle by trust domain

A TLS library with one flat trust store accepts any certificate signed by any CA in it. SPIFFE validation reads the ID first, takes the bundle of that ID's trust domain and walks the chain only to CAs in that bundle:

```python
spiffe_id = parse_spiffe_id(leaf.uris[0])
td = spiffe_id.trust_domain
bundle = bundles.get(td)
if bundle is None:
    return Verdict(False, "no-bundle", f"no trust bundle for {td} (have: {', '.join(sorted(bundles)) or 'none'}); the trust domain in the SVID selects the bundle", str(spiffe_id), td)
path, pool, current = [leaf], list(chain[1:]), leaf
for _ in range(MAX_CHAIN):
    anchor = next((cert for cert in bundle.authorities if signed_by(current, cert)), None)
    if anchor is not None:
        path.append(anchor)
        break
```

Before that, `leaf_problems()` applies the specification's leaf rules (exactly one URI SAN, no `CA:TRUE`, no `keyCertSign` or `cRLSign`, an ID with a path), and afterwards every certificate on the path is checked against the clock. The difference from a flat store is the impostor: a certificate for `spiffe://cc.test/web` signed by the CA of a federated partner, `other.test`. `openssl verify -CAfile both-cas.pem` says `OK`; `verify_svid()` says `unknown-authority: ... it was signed by an authority of other.test, which cannot vouch for cc.test`. Federation lets you accept the partner's workloads without letting the partner mint yours.

### Step 7: mutual TLS, then authorise by ID

`tls_context()` builds a context for either side with `verify_mode = CERT_REQUIRED`, `check_hostname = False` (a SPIFFE ID is not a hostname) and all bundles loaded as CAs, so OpenSSL checks the chain, the dates and the key usages during the handshake. The server then repeats the SPIFFE check on the peer's leaf and asks the authorizer:

```python
try:
    tls = context.wrap_socket(raw, server_side=True)
except (ssl.SSLError, OSError) as exc:
    reason, detail, code = tls_failure(exc)
    self.record(Decision("reject", "handshake", reason, detail, verify_code=code, peer=peer))
    return
with tls, contextlib.suppress(OSError, ValueError):
    tls.settimeout(self.idle_timeout)
    leaf = parse_certificate(tls.getpeercert(binary_form=True) or b"")
    verdict = authorize(verify_svid([leaf], identity.bundles, self.now()), self.authorizer)
    if not verdict.ok:
        self.record(Decision("reject", "authorize", verdict.reason, verdict.detail, verdict.spiffe_id, peer=peer))
        send_json(tls, {"error": f"permission denied: {verdict.detail}", "reason": verdict.reason})
        return
```

The handshake has 10 seconds; after it, an idle connection may stay open for 5 minutes, which Step 9 relies on. Every connection becomes one decision line with a stable reason: `expired`, `unknown-authority` and `not-yet-valid` come from OpenSSL's verify codes (10, 20, 9) during the handshake; `not-allowed` and the impostor's `unknown-authority` come from the authorization step. The client does the same in reverse: `--expect-server spiffe://cc.test/api` rejects a server that presents any other ID, which is how a client notices it was routed to the wrong service. The server speaks first after authorising, because in TLS 1.3 the client's handshake is already complete when the server rejects the client's certificate: the rejection arrives as an alert on the client's first read, and a client that had already sent data could see a TCP reset instead. Two local quirks to know when you read the logs: the system Python on macOS links LibreSSL, which reports any rejected client certificate as `no certificate returned` on the server side, so its client-side alert is the precise one; OpenSSL 3 servers report the verify code.

### Step 8: who is on the other end of the socket

The agent identifies a caller without asking it anything. On Linux a Unix socket can report the credentials of the process that connected:

```python
pid, uid, gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i")))
```

With the PID, `unix_selectors()` reads `/proc/PID/status` for the effective UID and GID and the supplementary groups, looks up the names, and optionally reads `/proc/PID/exe` for the binary's path, producing SPIRE's `unix` selectors: `unix:uid:1001`, `unix:user:app`, `unix:gid:...`, `unix:group:...`, `unix:supplementary_gid:...`, `unix:path:...`. A PID is only a number, so the caller could exit and another process could get the same PID while the agent is still reading `/proc`. `attest_unix()` records the process start time (field 22 of `/proc/PID/stat`) before and after, and checks that `/proc/PID` still belongs to the UID the socket reported:

```python
selectors = unix_selectors(creds, proc, discover_path, users, groups)
try:
    same = proc.starttime(creds.pid) == started and proc.owner(creds.pid) == creds.uid
except (OSError, ValueError, IndexError):
    same = False
if not same:
    raise AttestationError(f"pid {creds.pid} exited or was replaced while it was being attested; nothing issued")
```

SPIRE's peer tracker does the same with the start time and the directory owner. The registration entries are JSON (`{"spiffe_id": ..., "selectors": [...], "ttl": 300}`), and an entry matches when **all** its selectors are among the discovered ones, so `["unix:uid:1001"]` matches any process of that user and `["unix:uid:1001", "unix:path:/usr/bin/api"]` only that binary. A request must carry `"workload.spiffe.io": "true"`, the header the real Workload API requires so that a server-side request forgery cannot reach the socket through an HTTP client. Off Linux there is no `SO_PEERCRED`, and `agent --static-selectors` stands in for attestation.

### Step 9: short lifetimes, rotation, and a connection that outlives its certificate

`Rotator` holds the current identity and fetches a new one when half of the lifetime has passed; `serve --workload-api SOCKET` uses it, so each new connection gets the newest certificate while old connections keep the one they started with:

```python
def current(self) -> Identity:
    with self._lock:
        if self._identity is None or rotation_due(self._identity.svid.leaf, from_epoch(self.clock()), self.fraction):
            self._identity = self.fetch()
            self.rotations += 1
        return self._identity
```

The clock is injected, so the tests move time instead of waiting. One test does wait, because it is about real TLS: a client certificate valid for 2 seconds opens a connection, the client sends one message, waits 3.4 seconds and sends another on the same connection, which is answered; the reconnect fails with `expired`. `serve --enforce-expiry` adds the check TLS does not make: before each request it compares the peer certificate's `notAfter` with the clock and closes the connection with `reason: expired`.

### Step 10: run it

```bash
m() { python3 code/main.py "$@"; }
m id spiffe://cc.test/ns/shop/sa/web                     # trust domain cc.test, path /ns/shop/sa/web
m id spiffe://CC.test/web; echo "exit $?"                # invalid: lowercase only; exit 1
m ca --trust-domain cc.test --out ca-cc
m ca --trust-domain other.test --out ca-other
m issue --ca ca-cc --id spiffe://cc.test/api --out api
m issue --ca ca-cc --id spiffe://cc.test/web --ttl 300 --out web          # lifetime 310 s, rotate at half
m issue --ca ca-cc --id spiffe://cc.test/batch --out batch
m issue --ca ca-cc --id spiffe://cc.test/web --at @$(( $(date +%s) - 7200 )) --ttl 60 --out expired
m issue --ca ca-other --id spiffe://cc.test/web --any-trust-domain --out impostor
openssl verify -CAfile ca-cc/ca.pem -purpose sslclient web/svid.pem       # web/svid.pem: OK
openssl x509 -in web/svid.pem -noout -text | grep -A1 -E 'Alternative|Key Usage|Basic'
m verify impostor/svid.pem --bundle ca-cc/bundle.pem --bundle ca-other/bundle.pem; echo "exit $?"   # REJECT unknown-authority
m serve --svid api/svid.pem --key api/key.pem --bundle ca-cc/bundle.pem --allow spiffe://cc.test/web --listen 127.0.0.1:8443 &
for c in web batch expired; do
  m call 127.0.0.1:8443 --svid $c/svid.pem --key $c/key.pem --bundle ca-cc/bundle.pem --expect-server spiffe://cc.test/api
done                                                     # ACCEPT; REJECT not-allowed; REJECT expired
```

The agent and its client, with real attestation on Linux:

```bash
echo '[{"spiffe_id": "spiffe://cc.test/web", "selectors": ["unix:uid:'"$(id -u)"'"], "ttl": 300},
       {"spiffe_id": "spiffe://cc.test/batch", "selectors": ["unix:uid:0"], "ttl": 300}]' > entries.json
m agent --socket /tmp/miniid.sock --ca ca-cc --entries entries.json --federate ca-other/bundle.pem &
m fetch --socket /tmp/miniid.sock --out fetched          # spiffe://cc.test/web; svid.0.pem svid.0.key bundle.0.pem federated_bundle.0.0.pem
sudo python3 code/main.py fetch --socket /tmp/miniid.sock   # spiffe://cc.test/batch: same socket, different caller
```

The file names follow SPIRE's: `issue` writes what `spire-server x509 mint -write` writes (`svid.pem`, `key.pem`, `bundle.pem`), and `fetch` writes what `spire-agent api fetch x509 -write` writes, so the commands in Use It and the smoke test treat both issuers alike.

<RunTests />

## Use It

SPIRE (SPIFFE Runtime Environment, a CNCF graduated project) is the reference implementation: a server that holds the CA and the registration entries, and an agent per node that attests workloads and serves the Workload API. `code/smoke.sh` runs SPIRE 1.15.5 from its release tarball (checksum verified) next to `miniid` on a Linux runner and asserts that both issue the same identities to the same callers and that the two mTLS services make the same decisions with either issuer's certificates.

**Server and agent.** The server needs a datastore, a key manager and a node attestor; the agent needs to reach the server, attest itself and attest workloads. A join token is the simplest node attestation: a one-time secret the server generates and the agent presents once.

```hcl
server {
    bind_address = "127.0.0.1"
    bind_port = "8081"
    socket_path = "/tmp/spire-server/private/api.sock"
    trust_domain = "cc.test"
    data_dir = "/var/lib/spire/server"
    ca_key_type = "ec-p256"
    ca_ttl = "24h"
    default_x509_svid_ttl = "1h"
}
plugins {
    DataStore "sql" {
        plugin_data {
            database_type = "sqlite3"
            connection_string = "/var/lib/spire/server/datastore.sqlite3"
        }
    }
    KeyManager "memory" { plugin_data {} }
    NodeAttestor "join_token" { plugin_data {} }
}
```

```hcl
agent {
    data_dir = "/var/lib/spire/agent"
    trust_domain = "cc.test"
    server_address = "127.0.0.1"
    server_port = 8081
    socket_path = "/tmp/spire-agent/public/api.sock"
    insecure_bootstrap = true
}
plugins {
    KeyManager "memory" { plugin_data {} }
    NodeAttestor "join_token" { plugin_data {} }
    WorkloadAttestor "unix" { plugin_data {} }
}
```

`insecure_bootstrap` lets the agent trust whatever server answers the first time; in production you give it the server's bundle (`trust_bundle_path` or `trust_bundle_url`). The defaults are a 24-hour CA and 1-hour SVIDs, and the server warns when `ca_ttl` is too short to guarantee the SVID lifetime across a CA rotation.

```bash
S=/tmp/spire-server/private/api.sock; A=/tmp/spire-agent/public/api.sock
spire-server run -config server.conf &
spire-server healthcheck -socketPath $S                                    # Server is healthy.
TOKEN=$(spire-server token generate -socketPath $S -ttl 600 | awk '/^Token:/ {print $2}')
spire-agent run -config agent.conf -joinToken "$TOKEN" &
spire-agent healthcheck -socketPath $A                                     # Agent is healthy.
spire-server agent list -socketPath $S                                     # spiffe://cc.test/spire/agent/join_token/<token>
AGENT=spiffe://cc.test/spire/agent/join_token/$TOKEN
spire-server entry create -socketPath $S -parentID $AGENT -spiffeID spiffe://cc.test/web -selector unix:uid:$(id -u) -x509SVIDTTL 300
spire-server entry create -socketPath $S -parentID $AGENT -spiffeID spiffe://cc.test/batch -selector unix:uid:0 -x509SVIDTTL 300
spire-server entry show -socketPath $S
spire-agent api fetch x509 -socketPath $A -write ./web                     # svid.0.pem svid.0.key bundle.0.pem; uid matched web
sudo spire-agent api fetch x509 -socketPath $A -write ./batch              # the same socket hands root the batch identity
spire-agent api watch -socketPath $A                                       # the stream: a new SVID at about half of 310 s
spire-server bundle show -socketPath $S -format spiffe > bundle.json       # JWK Set: x509-svid and jwt-svid keys
spire-server x509 mint -socketPath $S -spiffeID spiffe://cc.test/api -ttl 1h -write ./api   # svid.pem key.pem bundle.pem, no attestation
```

The parent ID ties an entry to the agent that may hand it out: an entry parented to one node's agent is never issued on another node. A fetch from a caller that matches no entry fails with `PermissionDenied ... no identity issued`, which is the first thing to check when a workload "cannot get a certificate". `x509 mint` signs on the server without any attestation; it is a debugging and bootstrap tool, and access to the server's admin socket is therefore as sensitive as the CA key.

Point `miniid` at what SPIRE issued, and SPIRE's clients at what `miniid` issued:

```bash
python3 code/main.py inspect web/svid.0.pem     # spiffe://cc.test/web, subject C=US, O=SPIRE, 310 s, problems none
python3 code/main.py verify web/svid.0.pem --bundle cc.test=bundle.json --allow spiffe://cc.test/web
openssl verify -CAfile web/bundle.0.pem -purpose sslclient web/svid.0.pem
```

SPIRE's leaf also sets `keyEncipherment` and `keyAgreement`, which the specification allows, its subject is a fixed `C=US, O=SPIRE` because nothing reads it, and its lifetime is the entry's TTL plus the same 10-second backdate. The agent rotates when the SVID reaches half of its lifetime, with up to 10% of jitter either way so that thousands of workloads do not rotate in the same second, or earlier when `availability_target` is set.

**Delivering the SVID to the application.** Libraries such as go-spiffe and java-spiffe keep the Workload API stream open and swap certificates in memory. For software that reads files, `spiffe-helper` does the fetching: it writes the files and signals the process after each rotation, here nginx through its PID file, since nginx reloads its certificates on `SIGHUP`:

```hcl
agent_address = "/tmp/spire-agent/public/api.sock"
daemon_mode = true
cert_dir = "/etc/nginx/svid"
svid_file_name = "svid.pem"
svid_key_file_name = "svid_key.pem"
svid_bundle_file_name = "svid_bundle.pem"
pid_file_name = "/run/nginx.pid"
renew_signal = "SIGHUP"
```

It can also start the process itself with `cmd` and `cmd_args`; the arguments are split on spaces, not parsed like a shell, so quoting does not work there.

**On Kubernetes.** SPIRE's agent runs as a DaemonSet and attests nodes with `k8s_psat` (a projected ServiceAccount token the API server validates) and workloads with the `k8s` attestor, whose selectors (`k8s:ns:payments`, `k8s:sa:api`, `k8s:pod-label:app:api`) come from the kubelet for the PID on the socket. The SPIRE Controller Manager turns a `ClusterSPIFFEID` resource with a template such as `spiffe://{{ .TrustDomain }}/ns/{{ .PodMeta.Namespace }}/sa/{{ .PodSpec.ServiceAccountName }}` into entries for every matching pod, and the SPIFFE CSI driver mounts the agent's socket into pods. cert-manager's csi-driver-spiffe takes another route without SPIRE: a CSI volume in which the driver requests a certificate for `spiffe://TRUST_DOMAIN/ns/NAMESPACE/sa/SERVICE_ACCOUNT` from a cert-manager issuer, its approver checks that the request matches the pod's ServiceAccount, the key never leaves the node, and the files are renewed before expiry.

**Meshes do all of this for you.** Istio in ambient mode runs a per-node ztunnel that holds a certificate for each local pod's ServiceAccount (`spiffe://cluster.local/ns/NS/sa/SA` by default) and carries pod traffic in mutual TLS tunnels; an `AuthorizationPolicy` with `principals: ["cluster.local/ns/shop/sa/web"]` is your authorizer, written without the `spiffe://` prefix. Linkerd's proxies get certificates bound to the pod's ServiceAccount that expire after 24 hours and rotate automatically, and its `MeshTLSAuthentication` and `AuthorizationPolicy` resources allow callers by identity. Istio can take its workload certificates from SPIRE, and both meshes can chain their issuer to a CA you run. A rotated certificate is used for new connections; a connection that is already established is not authenticated again, which is the mid-connection behaviour from Step 9, and Envoy's `max_connection_duration` is the kind of setting that bounds it.

| What you built | Real tool | Note |
|---|---|---|
| `parse_spiffe_id()` | go-spiffe `spiffeid.FromString`, SPIRE's API validation | the same rules; reject instead of normalising |
| `new_authority()`, `issue_svid()` | SPIRE server CA (`ca_key_type`, `ca_ttl`, `default_x509_svid_ttl`), `spire-server x509 mint` | both backdate by 10 s and cap at the CA's expiry; SPIRE adds two key usages |
| `ecdsa_sign()` with RFC 6979 | Go's `crypto/ecdsa`, OpenSSL | constant time, hedged nonces |
| `Bundle.to_spiffe()`, `parse_bundle()` | `spire-server bundle show -format spiffe`, the federation endpoint | one JWK Set per trust domain with x509-svid and jwt-svid keys |
| `verify_svid()` | go-spiffe `x509svid.Verify` | the bundle is chosen by the SVID's trust domain |
| `Authorizer` | go-spiffe `tlsconfig.AuthorizeID` and `AuthorizeMemberOf`, Istio `AuthorizationPolicy`, Linkerd `MeshTLSAuthentication` | authentication without authorization is just encryption |
| `peer_credentials()`, `attest_unix()` | SPIRE's peer tracker and `unix` workload attestor | the same `SO_PEERCRED`, start time and owner checks |
| `matching_entries()` | `spire-server entry create -selector`, `ClusterSPIFFEID` | all of an entry's selectors must match |
| `WorkloadApi`, `fetch` | `spire-agent` Workload API (gRPC on a Unix socket), `spire-agent api fetch x509` | gRPC streams updates; the `workload.spiffe.io` header is required |
| `Rotator` | agent rotation at half-life with jitter, `spire-agent api watch`, `spiffe-helper` | rotation pushes; the workload never polls |
| `serve`, `call` | ghostunnel, Envoy with SDS, ztunnel, linkerd-proxy | the mesh terminates mTLS next to the application |
| `--enforce-expiry` | Envoy `max_connection_duration` | TLS alone never re-checks an established connection |

<Quiz stage="check" />

## Ship It

Two artifacts in `outputs/`:

- **`skill-workload-identity-rollout.md`**: a runbook for moving a service from network allowlists and shared keys to SPIFFE identities and mutual TLS. It inventories who calls the service, fixes the trust domain and the ID scheme, writes registration entries with selectors precise enough that only the intended workload matches, delivers SVIDs (library, `spiffe-helper` or a mesh), rolls out authorization in audit mode first, then enforces, monitors rotation and expiry, and ends with a fixed report.
- **`prompt-explain-mtls-failure.md`**: paste the client's error, the server's log line and optionally `openssl s_client` or `openssl x509 -text` output, and get which side rejected the connection, at which step (no SVID issued, chain or expiry during the handshake, or authorization by ID), the one change that fixes it, and the fixes that only hide it.

<ShipIt />

## Exercises

### Easy

Add jitter to rotation the way SPIRE does: `rotation_time(cert, fraction, jitter=0.1, rand=random.random)` returns a time within 10% of the half-life either side of it, so a fleet issued in the same second does not come back in the same second. Write the tests first with an injected `rand`: 0.0 gives the earliest time, 1.0 the latest, 0.5 exactly half-life, and a `Rotator` with a fixed `rand` refetches at the predicted moment and not one second earlier.

### Medium

Rotate the CA without breaking anyone. `ca --rotate DIR` creates a new key and certificate, writes a bundle with both CAs and `spiffe_sequence` incremented, and makes `issue` sign with the new one; `ca --retire DIR` removes the old CA from the bundle, but refuses while an SVID it signed could still be valid (track the longest TTL it issued). Write the tests first: an SVID from the old CA verifies against the two-CA bundle, a peer that only received the old bundle rejects new SVIDs until it refreshes, and retiring too early is an error that names the time when it becomes safe.

### Hard

Add JWT-SVIDs for traffic that cannot use mutual TLS, such as a request passing an HTTP load balancer: `issue-jwt --audience AUD` signs an ES256 JWT whose `sub` is the SPIFFE ID with a `kid` header, the bundle gains `jwt-svid` keys with that `kid`, and `verify-jwt` checks the algorithm against an allow-list, finds the key by `kid` in the bundle of the `sub`'s trust domain only, and checks `aud` and `exp`. Write the tests first: a token for another audience, an expired token, a `kid` from another trust domain's bundle and `alg: none` are all rejected. Then compare with `spire-agent api fetch jwt -audience AUD` and `spire-agent api validate jwt`, and write down in the docs why a JWT-SVID, unlike an X509-SVID, can be replayed by whoever receives it.

<Quiz stage="post" />

## Key Terms

| Term | Common misconception | Precise definition |
|---|---|---|
| SPIFFE ID | "It is a hostname or a DNS name for the service." | A URI `spiffe://trust-domain/path` naming a workload inside a trust domain, with strict syntax rules so IDs compare as strings. It says nothing about where the workload runs. |
| Trust domain | "Any CA we trust can sign identities for us." | One root of trust with its own signing authorities. Only the authorities in a trust domain's bundle may sign SVIDs whose ID is in that trust domain. |
| X509-SVID | "Any client certificate with the service name in it." | An X.509 certificate with exactly one URI SAN holding the SPIFFE ID, `CA:FALSE`, `digitalSignature`, no `keyCertSign` or `cRLSign`, and usually both `serverAuth` and `clientAuth`, with a lifetime of hours. |
| Trust bundle | "The list of CAs in the operating system's trust store." | The CA certificates of one trust domain, published as PEM or as a SPIFFE bundle (a JWK Set with a sequence number). Validators keep one bundle per trust domain and select it by the peer's ID. |
| Workload attestation | "The workload logs in to get its certificate." | The agent establishes the caller's identity from sources the caller does not control: the kernel (`SO_PEERCRED`, `/proc`) or the orchestrator (kubelet, cloud metadata). The result is a set of selectors. |
| Registration entry | "A certificate request stored on the server." | A rule mapping a set of selectors (and a parent agent) to a SPIFFE ID and TTL. A workload receives every ID whose entry's selectors are all among its own. |
| Workload API | "An HTTP endpoint that hands out certificates." | A local gRPC service on a Unix socket, served by the agent, that streams a caller's SVIDs, private keys and bundles and pushes replacements on rotation. It needs no credentials from the caller. |
| Mutual TLS | "If both sides present certificates, the connection is authorised." | A TLS handshake in which the client also presents and proves a certificate. It authenticates both ends; whether the peer may talk to you is a separate decision on its ID. |
| Federation | "Two trust domains merge their CAs into one store." | Exchanging bundles so that each side can validate the other's SVIDs, with each bundle used only for IDs in its own trust domain. |
| Rotation | "Renewing the certificate shortly before it expires." | Replacing the SVID once a fraction of its lifetime (half, in SPIRE, with jitter) has passed, while the old one is still valid, so expiry never happens in normal operation. |

## Further Reading

- [DevSecOps Fundamentals](https://cloudcaptain.io/docs/tools/devsecops/fundamentals): where workload identity sits next to secrets management, policy and supply-chain security.
- [Kubernetes Security](https://cloudcaptain.io/docs/tools/kubernetes/security): ServiceAccounts, NetworkPolicy and RBAC, the Kubernetes identities that SPIFFE IDs are usually built from.
- [Network Security](https://cloudcaptain.io/docs/tools/networking/security): the perimeter model that zero trust replaces, and where TLS fits in it.
- [SPIFFE ID specification](https://github.com/spiffe/spiffe/blob/main/standards/SPIFFE-ID.md): every syntax rule in `parse_spiffe_id()` and the definition of trust domains and SVIDs.
- [X509-SVID specification](https://github.com/spiffe/spiffe/blob/main/standards/X509-SVID.md): the extensions an SVID must carry and the leaf validation rules in `leaf_problems()`.
- [SPIFFE Trust Domain and Bundle](https://github.com/spiffe/spiffe/blob/main/standards/SPIFFE_Trust_Domain_and_Bundle.md): the JWK Set bundle format, sequence numbers and federation.
- [SPIFFE Workload API](https://github.com/spiffe/spiffe/blob/main/standards/SPIFFE_Workload_API.md): the RPCs, the stream semantics and the security header that `WorkloadApi` imitates.
- [SPIRE concepts](https://spiffe.io/docs/latest/spire-about/spire-concepts/): node and workload attestation, registration entries and parent IDs.
- [Configuring SPIRE Server](https://spiffe.io/docs/latest/deploying/spire_server/) and [SPIRE Agent](https://spiffe.io/docs/latest/deploying/spire_agent/): every option and CLI command used in Use It.
- [unix(7)](https://man7.org/linux/man-pages/man7/unix.7.html): `SO_PEERCRED` and what the kernel reports about the process on the other end of a Unix socket.
- [proc_pid_stat(5)](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html): the `starttime` field used to detect a reused PID.
- [RFC 6979](https://www.rfc-editor.org/rfc/rfc6979): deterministic ECDSA nonces and the P-256 test vectors the tests check.
- [NIST SP 800-207, Zero Trust Architecture](https://csrc.nist.gov/pubs/sp/800/207/final): the model behind "authenticate and authorise every request, whatever the network".
- [cert-manager csi-driver-spiffe](https://cert-manager.io/docs/usage/csi-driver-spiffe/): SPIFFE SVIDs for pods from a cert-manager issuer, without SPIRE.
- [Istio ambient mode](https://istio.io/latest/docs/ambient/overview/): ztunnel, per-node mTLS and identity-based authorization in a mesh.
- [spiffe-helper](https://github.com/spiffe/spiffe-helper): delivering rotating SVIDs as files to software that cannot speak the Workload API.
