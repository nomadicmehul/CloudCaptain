# A Secret Is a Lease, Not a String

*A good secret is issued per client, expires on its own, dies with its lease, and sits under keys that need a quorum to unseal.*

## Metadata

| Field | Value |
|---|---|
| Type | Build |
| Languages | Python |
| Prerequisites | [Every AWS API Call Is a Policy Evaluation](https://cloudcaptain.io/learn/cloud/iam-policy-evaluator) |
| Time | 120 minutes |
| Tools | OpenBao, bao, docker, PostgreSQL, kubectl |

<Quiz stage="pre" />

## The Problem

Thursday, 11:20. A researcher writes to the security inbox: a public storage bucket holds a laptop backup, and inside it is the `.env` file of the `orders` service, with `DATABASE_URL` pointing at the orders database as user `app_rw`. Six services share that user and that password. It also sits in a Kubernetes Secret in three namespaces, in two CI variables, and in the Git history of a repository that was "cleaned up" two years ago. Changing it means changing PostgreSQL and all of those copies in the same minute, or six services fail together, so the rotation is booked for Sunday night. For three days the leaked password keeps working.

The incident review then asks what happened during those three days, and nobody can answer. PostgreSQL's log shows `app_rw` connecting from the expected pods and from two addresses nobody recognises, but every service is `app_rw`, so a connection proves nothing about who made it. The Kubernetes Secret that "protected" the password is base64 in the API and plain text in every etcd backup. The CI variables were readable by any workflow in the organisation. The password was never one secret. It was a string copied into nine places, each with its own readers and none with an expiry date.

A secrets service turns the string into a lease. The `orders` pod authenticates and asks for a database credential; the service creates a PostgreSQL user for that request, valid for an hour, and records which token received it. A leaked credential stops working on its own schedule. A compromised pod is cut off by revoking its token, which drops every user created for it and nothing else. The audit log lists every request, with each secret value replaced by an HMAC so the log is not the next leak. Underneath, every stored value is encrypted under its own key, and the key that unlocks the store is split so that no single operator holds it. You are going to build all of that as `minivault`, an HTTP service that answers OpenBao's API, and then run one script of 96 requests against both and compare every answer.

## The Concept

Think of a hotel. The front desk never hands out copies of the master key. It programs a card for each guest that opens one room until checkout; a lost card is cancelled at the desk without changing a lock; the door system logs every opening by card number; and the master key sits in a safe that needs three of the five managers to open. A secrets service is that front desk for passwords, API keys and certificates.

**Versioned storage.** OpenBao's KV version 2 engine never overwrites. A write to `secret/data/app/config` adds version n + 1, and a read returns the newest version or `?version=N`. Deleting is soft: it stamps one version with a `deletion_time`, after which a read answers 404 with the metadata still attached, and `undelete` clears the stamp. `destroy` throws the ciphertext away for good. Under `secret/metadata/` the engine keeps the history (current and oldest version, the state of each one), and `max_versions` bounds it by dropping the oldest version as new ones arrive. A write that carries `options.cas` succeeds only if the current version is still the one the writer read, so two deploys cannot overwrite each other without noticing.

**Envelope encryption.** Every version is encrypted under a fresh 256-bit data key with ChaCha20-Poly1305, an authenticated cipher (AEAD). ChaCha20 turns the key, a 96-bit nonce and a block counter into a keystream with 20 rounds of 32-bit additions, rotations and XORs. Poly1305 treats the ciphertext as the coefficients of a polynomial and evaluates it modulo 2^130 - 5 at a secret point, which gives a 16-byte tag. Decryption checks the tag before it releases a single byte, so a flipped bit or a wrong key is an error, never garbage. The associated data is the secret's path and version, so a ciphertext copied to another path or version refuses to decrypt. The data key is then encrypted ("wrapped") by the active term of a keyring, the keyring is encrypted by the root key, and the root key by the unseal key. Rotating the keyring adds a term for new writes; rewrapping re-encrypts one 60-byte wrapped key per version and leaves the data alone. A nonce must never repeat under one key: a data key that encrypts exactly one value makes that trivial, and the keyring term wraps few enough keys to stay far below the 2^32 messages NIST allows under one key with random 96-bit nonces.

**Shamir's secret sharing.** To split the 32-byte unseal key into 5 shares with a threshold of 3, each byte becomes the constant term of a random polynomial of degree 2 over GF(2^8), the field of bytes in which addition is XOR and multiplication is taken modulo x^8 + x^4 + x^3 + x + 1 (the AES field). A share is the 32 values of those polynomials at one non-zero x, followed by that x: 33 bytes, which is exactly what OpenBao prints as an unseal key. Three points fix a polynomial of degree 2, so Lagrange interpolation at x = 0 gives every byte back. Two points fit every possible constant term equally well, so two shares reveal nothing at all. A sealed server holds only ciphertext; it rebuilds the unseal key in memory from enough shares, opens the root key and the keyring, and forgets the shares. Auto-unseal replaces the shares with a key in a cloud KMS or an HSM that the server asks to decrypt its root key at start.

**Tokens and policies.** Every request carries a token in the `X-Vault-Token` header. The server stores only a hash of each token, next to its policies, a TTL, an optional `explicit_max_ttl` that no renewal can pass, and its parent. Revoking a token revokes its children, their children and every lease any of them created; an orphan token has no parent and survives. A policy is a list of path rules with capabilities (`create`, `read`, `update`, `delete`, `list`, `sudo`, `deny`). For each request the server picks one rule: the exact path if there is one, otherwise the most specific glob (`*` at the end) or segment wildcard (`+` for one path segment), where a wildcard that starts later in the path beats one that starts earlier. Rules for the same path in several policies are merged as a union, and `deny` on that rule overrides everything else. A write counts as `create` when the secret does not exist yet and as `update` when it does, so a policy with only `create` lets a client write once.

**Dynamic secrets.** A database role holds SQL templates and TTLs. Reading `database/creds/ROLE` runs `CREATE ROLE "{{name}}" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'` with a fresh name and password, and returns both with a lease. Renewing the lease moves `VALID UNTIL` and the lease's expiry together, up to the role's `max_ttl`. When the lease expires or is revoked, the server runs the revocation statements: revoke the grants, drop the role. That credential has an owner, a deadline and an off switch; a string has none of the three.

**Audit.** Each request and response is written to the audit device with every string value replaced by `hmac-sha256:` and the HMAC under a key only the server holds. The log can be shipped anywhere. To find out whether a known value passed through, ask the server for its hash (`sys/audit-hash/DEVICE`) and search the log for that.

```mermaid
flowchart LR
  SH["5 unseal key shares"] -->|"any 3: Lagrange at x = 0"| UK["unseal key, in memory only"]
  UK -->|"decrypts"| RK["root key"]
  RK -->|"decrypts"| KR["keyring: term 1, term 2"]
  KR -->|"wraps"| DK["one data key per version"]
  DK -->|"ChaCha20-Poly1305, AAD = path and version"| CT["KV v2 ciphertext"]
  CL["client"] -->|"X-Vault-Token"| TK["token: policies, TTL, parent"]
  TK --> ACL{"most specific path rule"}
  ACL -->|"read secret/data/..."| CT
  ACL -->|"read database/creds/ROLE"| LS["lease: TTL, max TTL, owner"]
  LS -->|"CREATE ROLE ... VALID UNTIL"| DB[("PostgreSQL user")]
  LS -->|"expiry or revoke: DROP ROLE"| DB
  ACL --> AU["audit log: HMAC of every value"]
```

## Build It

`code/main.py` is `minivault`, one file of standard library Python with no dependencies. It is an HTTP server on `http.server` that answers a subset of OpenBao's API with the same paths, status codes and JSON shapes, plus offline commands for Shamir shares and policies, and a `replay` client that runs a scripted scenario against any server that speaks this API. The tests check the cipher against every test vector in RFC 8439, which `code/fixtures/rfc8439-vectors.json` holds as hex copied from the RFC text, and `code/fixtures/scenario.json` is the 96-step script that the smoke test runs against both OpenBao and minivault. The arithmetic here is not constant-time and the server has no TLS; it is for learning how the pieces fit, not for holding real secrets.

### Step 1: the ChaCha20 block function

The state is 16 words of 32 bits: four constants (`expand 32-byte k` in ASCII), eight words of key, a 32-bit block counter and three words of nonce, all little-endian. Ten double rounds each run the quarter round on the four columns and then on the four diagonals, and the original state is added back at the end so the rounds cannot be run backwards:

```python
def quarter_round(a: int, b: int, c: int, d: int) -> Tuple[int, int, int, int]:
    a = (a + b) & MASK32
    d = rotl32(d ^ a, 16)
    c = (c + d) & MASK32
    b = rotl32(b ^ c, 12)
    a = (a + b) & MASK32
    d = rotl32(d ^ a, 8)
    c = (c + d) & MASK32
    b = rotl32(b ^ c, 7)
    return a, b, c, d
```

```python
def chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    if len(key) != KEY_SIZE or len(nonce) != NONCE_SIZE:
        raise ValueError(f"ChaCha20 needs a 32-byte key and a 12-byte nonce, got {len(key)} and {len(nonce)}")
    state = list(SIGMA) + list(struct.unpack("<8L", key)) + [counter & MASK32] + list(struct.unpack("<3L", nonce))
    working = state[:]
    for _ in range(10):
        for a, b, c, d in QUARTER_ROUNDS:
            working[a], working[b], working[c], working[d] = quarter_round(working[a], working[b], working[c], working[d])
    return struct.pack("<16L", *((w + s) & MASK32 for w, s in zip(working, state)))
```

`chacha20_xor()` XORs the plaintext with consecutive blocks starting at counter 1 and refuses to let the 32-bit counter wrap, which would reuse keystream after 256 GiB. The RFC's section 2.3.2 state (`e4e7f110 15593bd1 ...`) and all five block vectors of appendix A.1 are tests.

### Step 2: Poly1305 and the AEAD construction

Poly1305 takes a 32-byte one-time key. The first half is `r`, with 22 bits cleared ("clamped") so the multiplication can be done fast in limbs; the second half is `s`. Every 16-byte block, with a `0x01` byte appended, is added to an accumulator that is then multiplied by `r` modulo 2^130 - 5. Python's integers make the field arithmetic one line; the RFC's appendix A.3 vectors exist precisely to catch the carry and reduction bugs that limb implementations get wrong, and they all pass here:

```python
def poly1305_mac(message: bytes, key: bytes) -> bytes:
    if len(key) != KEY_SIZE:
        raise ValueError("Poly1305 needs a 32-byte one-time key")
    r = int.from_bytes(key[:16], "little") & POLY1305_CLAMP
    s = int.from_bytes(key[16:], "little")
    accumulator = 0
    for offset in range(0, len(message), 16):
        block = message[offset:offset + 16] + b"\x01"
        accumulator = (accumulator + int.from_bytes(block, "little")) * r % POLY1305_PRIME
    return ((accumulator + s) & ((1 << 128) - 1)).to_bytes(16, "little")
```

The AEAD derives the one-time key from block 0 of the keystream, encrypts from block 1, and authenticates the associated data and the ciphertext, each padded to 16 bytes, followed by their two lengths. Decryption compares tags with `hmac.compare_digest` before it decrypts anything:

```python
def aead_tag(one_time_key: bytes, aad: bytes, ciphertext: bytes) -> bytes:
    mac_data = aad + pad16(aad) + ciphertext + pad16(ciphertext) + struct.pack("<QQ", len(aad), len(ciphertext))
    return poly1305_mac(mac_data, one_time_key)
```

```python
def aead_decrypt(key: bytes, nonce: bytes, sealed: bytes, aad: bytes = b"") -> bytes:
    if len(sealed) < TAG_SIZE:
        raise DecryptionError("ciphertext is shorter than the 16-byte tag")
    ciphertext, tag = sealed[:-TAG_SIZE], sealed[-TAG_SIZE:]
    if not hmac.compare_digest(aead_tag(poly1305_key_gen(key, nonce), aad, ciphertext), tag):
        raise DecryptionError("authentication tag mismatch: wrong key, wrong associated data, or the ciphertext was changed")
    return chacha20_xor(key, 1, nonce, ciphertext)
```

Section 2.8.2's "Ladies and Gentlemen of the class of '99" with its AAD `50515253c0c1c2c3c4c5c6c7` must produce the tag `1ae10b594f09e26a7e902ecbd0600691`, and appendix A.5's ciphertext must decrypt to the Internet-Drafts boilerplate. Changing one bit of the ciphertext, the tag, the AAD or the key raises `DecryptionError`.

### Step 3: envelope encryption with a keyring

`seal_box()` prepends a random 12-byte nonce to the AEAD output. `Keyring.seal()` gives every value its own data key, wraps that key under the active keyring term, and binds both to the context string (`secret/app/config#v3`) as associated data:

```python
def seal(self, plaintext: bytes, aad: str, rng: Callable[[int], bytes] = secrets.token_bytes) -> Dict[str, Any]:
    data_key = rng(KEY_SIZE)
    context = aad.encode("utf-8")
    return {
        "term": self.active,
        "wrapped_key": b64e(seal_box(self.terms[self.active], data_key, context + b"#data-key", rng)),
        "ciphertext": b64e(seal_box(data_key, plaintext, context, rng)),
    }
```

A stored version therefore looks like `{"term": 1, "wrapped_key": "...", "ciphertext": "..."}`: 60 bytes of wrapped key (nonce, key, tag) and the value's own ciphertext. `rotate()` adds a term and makes it active; old terms stay so old versions still open. `rewrap()` is why the envelope exists: it opens the 32-byte data key with the old term and seals it under the new one, and the ciphertext of the value, however large, is copied unchanged:

```python
def rewrap(self, record: Dict[str, Any], aad: str, rng: Callable[[int], bytes] = secrets.token_bytes) -> Dict[str, Any]:
    data_key = self.unwrap(record, aad)
    wrapped = seal_box(self.terms[self.active], data_key, aad.encode("utf-8") + b"#data-key", rng)
    return {"term": self.active, "wrapped_key": b64e(wrapped), "ciphertext": record["ciphertext"]}
```

### Step 4: Shamir's secret sharing over GF(2^8)

Multiplication in the AES field is shift-and-add (XOR), reducing by `0x11B` whenever the product overflows 8 bits; the inverse of a non-zero byte is its 254th power. FIPS-197's worked examples are tests: `{57}·{83} = {c1}`, `{57}·{13} = {fe}` and the inverse of `{53}` is `{ca}`.

```python
def gf_mul(a: int, b: int) -> int:
    product = 0
    while b:
        if b & 1:
            product ^= a
        a <<= 1
        if a & 0x100:
            a ^= GF_REDUCTION
        b >>= 1
    return product
```

Splitting draws a random polynomial per secret byte and evaluates it at shuffled, distinct, non-zero x coordinates (x = 0 would be the secret itself). The share layout is OpenBao's: the y bytes, then x:

```python
xs = shuffled_x_coordinates(randbelow)[:shares]
out = [bytearray(len(secret) + 1) for _ in range(shares)]
for part, x in zip(out, xs):
    part[-1] = x
for index, value in enumerate(secret):
    coefficients = [value] + list(rng(threshold - 1))
    for part, x in zip(out, xs):
        part[index] = gf_poly_eval(coefficients, x)
return [bytes(part) for part in out]
```

Combining is Lagrange interpolation at x = 0, where subtraction is XOR, so each basis term is `xj / (xi + xj)`:

```python
def gf_interpolate_at_zero(xs: Sequence[int], ys: Sequence[int]) -> int:
    result = 0
    for i, (xi, yi) in enumerate(zip(xs, ys)):
        basis = 1
        for j, xj in enumerate(xs):
            if i != j:
                basis = gf_mul(basis, gf_div(xj, xi ^ xj))
        result ^= gf_mul(yi, basis)
    return result
```

`shamir_combine()` refuses fewer than two parts, parts of different lengths and duplicate x coordinates, with OpenBao's error messages. The test `test_a_hand_computed_polynomial_is_interpolated_back_to_its_constant_term` evaluates `0x42 + x + 2x²` at 1, 2 and 3 by hand (`0x41`, `0x48`, `0x4B`) and gets `0x42` back.

### Step 5: sealed until a quorum arrives

`initialize()` creates the unseal key, the root key and the keyring, seals the root key under the unseal key and the keyring under the root key, splits the unseal key, prints the shares and forgets them. A server started from its state file is sealed and answers 503 to everything except `sys/seal-status`, `sys/health`, `sys/init` and `sys/unseal`. Each share submitted to `sys/unseal` is collected; when the threshold is reached they are combined and used at once:

```python
parts, self.unseal_parts = self.unseal_parts, []
try:
    combined = parts[0] if threshold == 1 else shamir_combine(parts)
    material = json.loads(open_box(combined, b64d(self.sealed_root), b"minivault/root-key"))
    root_key = b64d(material["root_key"])
    keyring = Keyring.from_json(json.loads(open_box(root_key, b64d(self.sealed_keyring), b"minivault/keyring")))
except (InputError, DecryptionError, ValueError, KeyError) as exc:
    raise ApiError(400, f"unseal failed: the combined key does not decrypt the root key ({exc})") from None
self.root_key, self.keyring = root_key, keyring
self.audit = AuditLog(b64d(material["audit_key"]), self.audit_path)
self.sealed = False
```

Shares from a different initialization, or one wrong share among the three, combine into a key that fails the Poly1305 check on the root key: the server answers 400, resets the progress counter and stays sealed. It never learns which share was wrong, which is one reason production setups encrypt each share to its holder with PGP and keep a record of who holds which.

### Step 6: KV version 2

A write checks the check-and-set option, encrypts the data under a new version number and trims history. The trimming copies OpenBao's own arithmetic, including its quirk that `oldest_version` stays 0 until the first version is actually dropped:

```python
options = call.body.get("options") or {}
if "cas" in options:
    try:
        cas = int(options["cas"])
    except (TypeError, ValueError):
        raise ApiError(400, "error parsing check-and-set parameter") from None
    if cas != meta["current_version"]:
        raise ApiError(400, "check-and-set parameter did not match the current version")
elif engine["config"]["cas_required"] or meta["cas_required"]:
    raise ApiError(400, "check-and-set parameter required for this call")
number = meta["current_version"] + 1
record = self.keyring.seal(json.dumps(data).encode("utf-8"), version_aad(mount, key, number), self.rng)
meta["current_version"] = number
meta["versions"][str(number)] = {"created_time": now, "deletion_time": None, "destroyed": False, "record": record}
meta["updated_time"] = now
limit = max(meta["max_versions"], engine["config"]["max_versions"]) or DEFAULT_MAX_VERSIONS
if number - meta["oldest_version"] >= limit:
    last = number - limit
    for old in range(meta["oldest_version"], last + 1):
        meta["versions"].pop(str(old), None)
    meta["oldest_version"] = last + 1
engine["keys"][key] = meta
```

A read of a deleted or destroyed version is a 404 that still carries the version's metadata, so a client can tell "never existed" (404 with `{"errors": []}`) from "deleted at 10:41":

```python
deleted = version["deletion_time"] is not None and version["deletion_time"] <= self.clock()
if deleted or version["destroyed"]:
    return Response(404, logical({"data": None, "metadata": metadata}))
data = json.loads(self.keyring.open(version["record"], version_aad(mount, key, number)))
return ok({"data": data, "metadata": metadata})
```

`destroy` sets `destroyed` and drops the record, so nothing, including the keyring, can bring the value back. Listing (`LIST secret/metadata/app`) returns the immediate children of a prefix, folders with a trailing slash.

### Step 7: tokens with TTLs and a family tree

A token is `s.` followed by 24 random letters and digits. The server keeps a SHA-256 of it, so its own state file does not contain a usable token. Expiry is checked on every lookup, so an expired token fails at once even before the background reaper removes it:

```python
def lookup_token(self, token_id: str) -> Optional[Token]:
    if not token_id:
        return None
    token = self.tokens.get(token_hash(token_id))
    if token is None or token.expired(self.clock()):
        return None
    return token
```

Renewal extends the TTL by the requested increment but never past `creation_time + explicit_max_ttl` (or the 768-hour system maximum). Revocation walks the tree: children first, then the token's leases, then the token. `revoke-orphan` (a root-protected path that needs `sudo`) clears the children's parent instead:

```python
def revoke_token(self, id_hash: str, orphan_children: bool = False) -> None:
    token = self.tokens.get(id_hash)
    if token is None:
        return
    for child_hash, child in list(self.tokens.items()):
        if child.parent == id_hash:
            if orphan_children:
                child.parent = ""
            else:
                self.revoke_token(child_hash)
    for lease in [lease for lease in self.leases.values() if lease.token_hash == id_hash]:
        self.revoke_lease_quietly(lease)
    self.tokens.pop(id_hash, None)
```

A non-root token can create children only with a subset of its own policies (`child policies must be subset of parent`), and a token with `num_uses` is revoked after its last use.

### Step 8: one rule decides

`compile_rules()` parses the HCL subset (`path "..." { capabilities = [...] }`, with comments) or the JSON form, sorts each pattern into exact, prefix (`*` at the end) or segment (`+`) rules, and refuses unknown capabilities and the forbidden `+*`. `ACL` merges rules for the same pattern across policies, with `deny` absorbing everything. Lookup takes the exact rule first, then the best wildcard. The sort key below is OpenBao's priority order: where the first wildcard starts, then exact segments before a trailing glob, then fewer `+`, then length:

```python
parts = path.split("/")
for full, permission in self.tables["segment"].items():
    first = full.index("+")
    current, is_prefix = (full[:-1], True) if full.endswith("*") else (full, False)
    acl_parts = current.split("/")
    if len(parts) < len(acl_parts) or (not is_prefix and len(parts) != len(acl_parts)):
        continue
    wildcards, matched = 0, True
    for index, acl_part in enumerate(acl_parts):
        if acl_part == "+":
            wildcards += 1
        elif acl_part == parts[index]:
            continue
        elif is_prefix and index == len(acl_parts) - 1 and parts[index].startswith(acl_part):
            continue
        else:
            matched = False
            break
    if matched:
        candidates.append(((first, not is_prefix, -wildcards, len(current), current), permission))
if not candidates:
    return None
return max(candidates, key=lambda item: item[0])[1]
```

So for `secret/data/app/shared`, a policy with both `secret/data/app/*` and `secret/data/+/shared` uses the glob: its wildcard starts at character 16, the `+` at 12. Before the check, `dispatch()` turns an `update` into a `create` when the target does not exist yet, the way OpenBao's existence check does:

```python
route = self.resolve(path)
if route is not None and operation == "update" and route.exists is not None and not route.exists():
    operation = "create"
trace["operation"] = operation
if not acl.allows(path, operation):
    raise ApiError(403, "permission denied")
```

### Step 9: a database credential is a lease

The database engine runs a role's statements through an injected executor: `FakeDatabase` keeps users (with salted SHA-256 verifiers and `VALID UNTIL`) in memory or in a JSON file, and `CommandDatabase` pipes the statements into a command such as `docker exec -i pg psql -v ON_ERROR_STOP=1 -U postgres`, redacting `PASSWORD '...'` from any error it reports. Issuing a credential is a `CREATE ROLE` and a lease that remembers which token asked:

```python
now = self.clock()
max_ttl = role["max_ttl"] or SYSTEM_MAX_TTL
ttl = min(role["default_ttl"] or SYSTEM_MAX_TTL, max_ttl)
username = f"v-{call.token.display_name[:8]}-{role_name[:8]}-{random_string(20)}-{int(now)}"[:63]
password = random_string(20)
statements = render_statements(role["creation_statements"], name=username, username=username, password=password, expiration=sql_expiration(now + ttl))
self.database.execute(statements)
lease = Lease(f"{mount}creds/{role_name}/{random_string(24)}", mount, role_name, username, call.token.id_hash, now, now + ttl, now + max_ttl)
self.leases[lease.lease_id] = lease
return ok({"username": username, "password": password}, lease_id=lease.lease_id, lease_duration=ttl, renewable=True)
```

The background ticker, four times a second, revokes expired tokens and leases; a lease whose revocation fails keeps its error (`revoke_error` in `sys/leases/lookup`) and is retried five seconds later, rather than being forgotten while the database user lives on:

```python
def tick(self) -> bool:
    if self.sealed:
        return False
    now = self.clock()
    changed = False
    for id_hash, token in list(self.tokens.items()):
        if id_hash in self.tokens and token.expired(now):
            self.revoke_token(id_hash)
            changed = True
    for lease in list(self.leases.values()):
        if lease.lease_id in self.leases and lease.expire_time <= now and lease.retry_at <= now:
            self.revoke_lease_quietly(lease)
            changed = True
    if changed:
        self.save()
    return changed
```

### Step 10: an audit log that is not a leak

```python
def scrub(self, value: Any) -> Any:
    if isinstance(value, str):
        return self.hash(value)
    if isinstance(value, dict):
        return {k: self.scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [self.scrub(v) for v in value]
    return value
```

`write_audit()` logs a request entry and a response entry for every authenticated call, with the client token, accessor and every string in the request and response data passed through `hash()`, an HMAC-SHA256 under a 32-byte key that is sealed with the root key. The same plaintext always gives the same hash, which is what makes `sys/audit-hash/file` useful and also why a low-entropy value such as a four-digit PIN can still be guessed from the log by anyone who can call that endpoint.

### Step 11: run it

```bash
cd curriculum/phases/11-security/01-secrets-management
python3 code/main.py server --dev --listen 127.0.0.1:8200 --fake-db /tmp/users.json --audit-log /tmp/audit.log &
#   minivault dev server on http://127.0.0.1:8200
#     root token: root
#     unseal key (base64): eVpYM944bvEA5DAaRbKjF7AhyZtVZtNuZmrnP0Hu/Ms=
curl -s -H 'X-Vault-Token: root' -d '{"data": {"password": "pw-1"}}' http://127.0.0.1:8200/v1/secret/data/app/config
#   {"request_id": "...", "data": {"version": 1, "created_time": "2026-11-02T01:12:11.116944Z", "deletion_time": "", "destroyed": false, ...}}
python3 code/main.py replay --addr http://127.0.0.1:8200 --script code/fixtures/scenario.json --scenario kv
#   FAIL  kv         200  write version 1  data.version = 2, want 1
#   ...
```

The replay fails on purpose: the `curl` already wrote version 1, so every version number in the script is one off. Restart the server and run the script on a clean store, then issue a database credential and watch it work and stop working:

```bash
python3 code/main.py replay --addr http://127.0.0.1:8200 --script code/fixtures/scenario.json --scenario kv --scenario policy --scenario tokens
#   PASS  kv         200  write version 1  data.version=1, data.destroyed=false
#   PASS  policy     403  deny beats the glob
#   PASS  tokens     403  it stops working at expiry
#   81/81 steps as expected against http://127.0.0.1:8200
python3 code/main.py replay --addr http://127.0.0.1:8200 --script code/fixtures/scenario.json --scenario db-setup \
  --var 'db_config={"plugin_name": "postgresql-database-plugin", "allowed_roles": "readonly,short"}'
curl -s -H 'X-Vault-Token: root' http://127.0.0.1:8200/v1/database/creds/readonly > creds.json
#   "lease_id": "database/creds/readonly/mrJ1wkqqyuTCZcgZF9CfR1mI", "lease_duration": 3600, "renewable": true,
#   "data": {"username": "v-token-readonly-3421Q86wj1hAGPGf1fqd-1793581931", "password": "i2MiShl0VFObFUkCpa0G"}
python3 code/main.py fake-db login --file /tmp/users.json --username v-token-readonly-3421Q86wj1hAGPGf1fqd-1793581931 --password i2MiShl0VFObFUkCpa0G
#   logged in as v-token-readonly-3421Q86wj1hAGPGf1fqd-1793581931
curl -s -X PUT -H 'X-Vault-Token: root' -d '{"lease_id": "database/creds/readonly/mrJ1wkqqyuTCZcgZF9CfR1mI"}' http://127.0.0.1:8200/v1/sys/leases/revoke
python3 code/main.py fake-db login --file /tmp/users.json --username v-token-readonly-3421Q86wj1hAGPGf1fqd-1793581931 --password i2MiShl0VFObFUkCpa0G
#   password authentication failed for user "v-token-readonly-3421Q86wj1hAGPGf1fqd-1793581931"   (exit 1)
curl -s -X POST -H 'X-Vault-Token: root' -d '{"input": "pw-1"}' http://127.0.0.1:8200/v1/sys/audit-hash/file
#   {"hash": "hmac-sha256:c5084461ed8f...", ...}
grep -c pw-1 /tmp/audit.log
#   0
```

The audit log's first entry is the write from the `curl`, with the password replaced by that same hash and the operation recorded as `create`, because the secret did not exist yet. Now the sealed mode, with a state file and five shares:

```bash
python3 code/main.py operator init --data /tmp/mv --shares 5 --threshold 3
#   Unseal Key 1: BwKS+y1HS++dsQvbRLU2RBWTnw67QPlVTy8X9fjFlD6D
#   ...
#   Initial Root Token: s.<24 random letters and digits>
python3 code/main.py server --data /tmp/mv --listen 127.0.0.1:8201 &
#   sealed: POST 3 of 5 unseal key shares to /v1/sys/unseal
curl -s -X PUT -d '{"key": "BwKS+y1HS++dsQvbRLU2RBWTnw67QPlVTy8X9fjFlD6D"}' http://127.0.0.1:8201/v1/sys/unseal
#   {"type": "shamir", "initialized": true, "sealed": true, "t": 3, "n": 5, "progress": 1, ...}
python3 code/main.py shamir combine SHARE_2 SHARE_3 SHARE_5     # 42632c99c6d3b526...
python3 code/main.py shamir combine SHARE_1 SHARE_4 SHARE_5     # 42632c99c6d3b526...  any three agree
python3 code/main.py shamir combine SHARE_1 SHARE_4             # f156a637c76cb8f0...  two give noise
python3 code/main.py policy check --policy app=app.hcl --path secret/data/app/admin --path secret/data/app/shared
#   DENY  read   secret/data/app/admin  capabilities=deny  exact rule 'secret/data/app/admin' says deny, ...
#   ALLOW read   secret/data/app/shared  capabilities=read,update,create  prefix rule 'secret/data/app/*' grants read
```

After the third share the server answers `"sealed": false`, a write to `secret/data/db` returns 200, and `grep` finds no trace of the value in `/tmp/mv/minivault.json` (mode `0600`): the file holds the sealed root key, the sealed keyring, token hashes, policies, and per-version `term`, `wrapped_key` and `ciphertext`. Exit codes are 0 for success, 1 for a failed replay, a denied `policy check` or a failed login, and 2 for input minivault cannot use; every command that prints a result takes `--json`.

<RunTests />

## Use It

[OpenBao](https://openbao.org) is the open-source fork of HashiCorp Vault, made after Vault moved to the Business Source License in 2023 and governed under the Linux Foundation; its API, CLI and storage format are Vault's, with `bao` in place of `vault` and `BAO_ADDR`/`BAO_TOKEN` in place of the `VAULT_` variables. The smoke test pins OpenBao 2.7.2 (`openbao/openbao:2.7.2`) and PostgreSQL 18.6 (`postgres:18.6-alpine`).

**A dev server with an audit device.** Dev mode is in memory, initialized, unsealed, with KV v2 mounted at `secret/` and a root token you choose. Current OpenBao releases refuse to enable audit devices over the API (`cannot enable audit device via API`); they are declared in the server configuration, which the image accepts as JSON in `BAO_LOCAL_CONFIG`. Keep the `description`: in 2.7 a device block whose only key is `options` fails to load with `audit type must be specified`.

```bash
docker network create cc-secrets
docker run -d --name cc-pg --network cc-secrets -e POSTGRES_PASSWORD=pg-admin-pw postgres:18.6-alpine
docker run -d --name cc-bao --network cc-secrets -p 127.0.0.1:8200:8200 \
  -e BAO_DEV_ROOT_TOKEN_ID=root \
  -e BAO_LOCAL_CONFIG='{"audit": [{"file": {"file": {"description": "lab audit log", "options": {"file_path": "/openbao/logs/audit.log"}}}}]}' \
  openbao/openbao:2.7.2
bao() { docker exec -i -e BAO_ADDR=http://127.0.0.1:8200 -e BAO_TOKEN="${BAO_TOKEN:-root}" cc-bao bao "$@"; }
bao status                                                 # Sealed false, Total Shares 1, Storage Type inmem
```

**KV version 2.** The CLI inserts `data/` and `metadata/` into the path for you; policies, the HTTP API and `minivault replay` use the full API path:

```bash
bao kv put secret/app/config user=app password=pw-1        # version 1
bao kv put secret/app/config user=app password=pw-2        # version 2
bao kv put -cas=1 secret/app/config password=pw-x          # Error ... check-and-set parameter did not match the current version
bao kv get -version=1 -field=password secret/app/config    # pw-1
bao kv delete secret/app/config                            # soft delete of version 2
bao kv undelete -versions=2 secret/app/config
bao kv destroy -versions=1 secret/app/config
bao kv metadata put -max-versions=5 secret/app/config
bao kv metadata get secret/app/config                      # current_version, oldest_version, each version's deletion_time and destroyed
bao kv list secret/app
python3 code/main.py replay --addr http://127.0.0.1:8200 --script code/fixtures/scenario.json --scenario kv   # on a fresh server: 27/27
```

**Policies and tokens.**

```bash
bao policy write app - <<'EOF'
path "secret/data/app/*"      { capabilities = ["create", "read", "update"] }
path "secret/data/app/admin"  { capabilities = ["deny"] }
path "secret/metadata/app/*"  { capabilities = ["list", "read"] }
EOF
APP=$(bao token create -policy=app -ttl=1h -field=token)
bao token capabilities "$APP" secret/data/app/config       # create, read, update
bao token capabilities "$APP" secret/data/app/admin        # deny
BAO_TOKEN=$APP bao kv get secret/app/admin                 # Code: 403 ... permission denied
BAO_TOKEN=$APP bao token lookup                            # policies [app default], ttl counting down
SHORT=$(bao token create -policy=app -ttl=10m -explicit-max-ttl=1h -field=token)
bao token renew -increment=2h "$SHORT"                     # token_duration about 1h, with a warning that the TTL was capped
bao token revoke "$APP"                                    # the token, its children and their leases
bao token revoke -mode=orphan "$SHORT"                     # only this token; its children become orphans
```

**Dynamic database credentials.** The database engine connects to PostgreSQL with an admin account, creates one user per lease and drops it at expiry. `bao write database/rotate-root/pg` then changes that admin password to one only OpenBao knows:

```bash
docker exec cc-pg psql -U postgres -c 'CREATE TABLE orders (id int)'
bao secrets enable database
bao write database/config/pg plugin_name=postgresql-database-plugin allowed_roles=readonly \
  connection_url='postgresql://{{username}}:{{password}}@cc-pg:5432/postgres?sslmode=disable' \
  username=postgres password=pg-admin-pw
bao write database/roles/readonly db_name=pg default_ttl=1h max_ttl=24h \
  creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT SELECT ON ALL TABLES IN SCHEMA public TO \"{{name}}\";"
bao read -format=json database/creds/readonly > creds.json  # lease_id database/creds/readonly/..., lease_duration 3600
PGIP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' cc-pg)
docker exec -e PGPASSWORD="$(jq -r .data.password creds.json)" cc-pg \
  psql -h "$PGIP" -U "$(jq -r .data.username creds.json)" -d postgres -tAc 'select current_user'
bao lease renew -increment=30m "$(jq -r .lease_id creds.json)"
bao lease revoke "$(jq -r .lease_id creds.json)"           # the same psql now fails: password authentication failed
```

The login goes to the container's own network address on purpose: the official image trusts local and `127.0.0.1` connections without a password, so a check over the loopback would succeed with any password and prove nothing.

**Initialize and unseal a real server.** Outside dev mode the server starts uninitialized. `operator init` prints the shares once; production deployments pass `-pgp-keys` so each share is encrypted to one holder, or use auto-unseal with a cloud KMS or an HSM. The share format is the one minivault produces, so minivault can combine OpenBao's shares, and OpenBao accepts shares that minivault split from the same unseal key, which is what `bao operator rekey` does with a new random key:

```bash
docker run -d --name cc-bao-prod -p 127.0.0.1:8210:8200 \
  -e BAO_LOCAL_CONFIG='{"storage": {"inmem": {}}, "listener": [{"tcp": {"address": "0.0.0.0:8200", "tls_disable": true}}]}' \
  openbao/openbao:2.7.2 server
docker exec -e BAO_ADDR=http://127.0.0.1:8200 cc-bao-prod bao operator init -key-shares=5 -key-threshold=3 -format=json > init.json
docker exec -e BAO_ADDR=http://127.0.0.1:8200 cc-bao-prod bao operator unseal "$(jq -r '.unseal_keys_b64[0]' init.json)"   # Unseal Progress 1/3
python3 code/main.py shamir combine $(jq -r '.unseal_keys_b64[0,2,4]' init.json)      # the 32-byte unseal key, in hex
python3 code/main.py shamir combine $(jq -r '.unseal_keys_b64[1,3,4]' init.json)      # the same key
```

`tls_disable` and `inmem` storage are for the lab only; a real server listens on TLS and uses integrated (Raft) storage, which OpenBao encrypts entry by entry under its keyring, the same layering as Step 5.

**Kubernetes.** A Kubernetes Secret is a base64 encoding, not encryption. The API returns it to anyone with `get` on Secrets in the namespace, anyone who can create a pod there can mount it, and etcd stores it in plain text unless the API server has an `EncryptionConfiguration`:

```bash
kubectl create namespace cc-secrets
kubectl -n cc-secrets create secret generic app-db --from-literal=password=pw-in-etcd
kubectl -n cc-secrets get secret app-db -o jsonpath='{.data.password}' | base64 -d        # pw-in-etcd
kubectl -n kube-system exec etcd-cc-lab-control-plane -- etcdctl --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt --cert=/etc/kubernetes/pki/etcd/server.crt --key=/etc/kubernetes/pki/etcd/server.key \
  get /registry/secrets/cc-secrets/app-db --print-value-only | strings | grep pw-in-etcd   # on kind: plain text
```

The API server's `EncryptionConfiguration` with a `kms` v2 provider is envelope encryption again: a data key encrypts each resource and the KMS wraps the data key. Environment variables are worse than Secret volumes: they are copied into every child process, readable in `/proc/PID/environ` by the same user, shown by `docker inspect`, and printed by crash reporters and debug endpoints.

Two operators keep secrets out of Git. The [External Secrets Operator](https://external-secrets.io/latest/) reads from OpenBao (through its `vault` provider), cloud secret managers and others, and writes ordinary Kubernetes Secrets that it refreshes on an interval:

```yaml
apiVersion: external-secrets.io/v1
kind: SecretStore
metadata:
  name: openbao
  namespace: orders
spec:
  provider:
    vault:
      server: "http://openbao.openbao.svc:8200"
      path: "secret"
      version: "v2"
      auth:
        kubernetes:
          mountPath: "kubernetes"
          role: "orders"
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: orders-db
  namespace: orders
spec:
  refreshInterval: "15m"
  secretStoreRef:
    name: openbao
    kind: SecretStore
  target:
    name: orders-db
  data:
    - secretKey: password
      remoteRef:
        key: app/config
        property: password
```

[Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets) goes the other way: `kubeseal` encrypts a Secret with the controller's public key (a random AES-256-GCM key wrapped with RSA-OAEP, envelope encryption once more) into a `SealedSecret` that is safe to commit, and only the controller in that cluster can turn it back into a Secret. Both still end as a Kubernetes Secret, so RBAC on Secrets and encryption at rest still matter; neither gives you leases or per-client credentials, which is what a secrets service adds.

| What you built | Real tool | Note |
|---|---|---|
| `aead_encrypt()`, `aead_decrypt()` | the AEAD in TLS 1.3, WireGuard, SSH; AES-256-GCM in OpenBao's barrier | constant-time implementations; never reuse a nonce under one key |
| `Keyring.seal()`, `rewrap()`, `sys/rotate` | OpenBao keyring terms, the Transit engine's `rewrap`, cloud KMS envelope encryption | OpenBao encrypts every storage entry with the active term; per-value data keys are what Transit and KMS hand out |
| `shamir_split()`, `shamir_combine()` | `bao operator init -key-shares -key-threshold`, `bao operator rekey` | the same 33-byte share format; `-pgp-keys` encrypts each share to its holder |
| `submit_unseal_key()`, `sys/seal-status` | `bao operator unseal`, auto-unseal with a KMS or HSM | progress resets after a combination that fails |
| `kv_write()`, `kv_read()`, `kv_list()` | `bao kv put/get/list/delete/undelete/destroy/metadata` | the CLI adds `data/` and `metadata/` to paths |
| `issue_token()`, `renew_token()`, `revoke_token()` | `bao token create/renew/revoke`, `-mode=orphan` | real clients get tokens from auth methods (Kubernetes, OIDC, AppRole, cloud IAM) |
| `compile_rules()`, `ACL` | `bao policy write`, `bao token capabilities` | OpenBao also supports `allowed_parameters`, `required_parameters` and templated paths |
| `db_creds()`, `revoke_lease()`, `tick()` | the database secrets engine, `bao lease renew/revoke`, `rotate-root` | static roles rotate the password of an existing user instead |
| `AuditLog`, `sys/audit-hash/file` | file, socket and syslog audit devices declared in the server config | OpenBao fails a request when no enabled audit device can record it |
| `minivault replay` | `bao`, `curl`, any HTTP client | the same requests and the same JSON against either server |

<Quiz stage="check" />

## Ship It

Two artifacts in `outputs/`:

- **`skill-secrets-rotation.md`**: a runbook for rotating a secret that has leaked or reached its age limit: classify it (static string, versioned KV secret, dynamic lease, token, unseal key), contain it by revoking leases and tokens rather than waiting, rotate in the order that keeps the service up (new version or new credential first, consumers second, old one revoked last), confirm with the audit log by hash, and move the static string to a dynamic secret so the next rotation is an expiry. It includes the OpenBao commands for each step, a decision table and a fixed report.
- **`prompt-review-secret-handling.md`**: paste a Deployment, a Helm values file, a CI workflow, a Terraform file or a piece of application code and get each secret it touches classified by where it lives and who can read it, the specific leak paths (environment variables, logs, images, Git history, base64 Secrets, long-lived tokens), and the change that turns each into a scoped, expiring credential.

<ShipIt />

## Exercises

### Easy

Add `delete_version_after` to KV metadata and mount config: a version written with it set gets a `deletion_time` of `created_time + delete_version_after`, a read after that moment returns the 404-with-metadata response, and `undelete` sets a new deletion time from the moment of the undelete, as OpenBao does. Write the tests first on the fake clock: a version is readable one second before its deadline and not one second after, metadata shows the future `deletion_time`, the key-level value wins over the mount's, and `"0s"` disables it.

### Medium

Implement `sys/rekey/init`, `sys/rekey/update` and `sys/rekey/init` with `DELETE` to cancel: a rekey collects a quorum of the current shares, generates a new unseal key, re-seals the root key under it, and returns new shares with a new share count and threshold, without touching the keyring or any data. Tests first: data written before the rekey reads after a restart with the new shares, the old shares are refused with a 400, a rekey with a wrong share leaves the old shares valid, and progress is reported the way `sys/unseal` reports it.

### Hard

Add response wrapping. A request with the header `X-Vault-Wrap-TTL: 60s` returns, instead of the secret, a single-use wrapping token whose cubbyhole holds the real response; `sys/wrapping/unwrap` returns the response once and deletes it, `sys/wrapping/lookup` shows the creation path and TTL without consuming it, and the wrapping token expires on its TTL. Tests first: a second unwrap fails, an expired wrapping token fails, the audit log never contains the wrapped value, and a receiver can detect interception because its own unwrap fails after someone else's succeeded. Then write two sentences on why CI systems hand a wrapped token, not a secret, to a new job.

<Quiz stage="post" />

## Key Terms

| Term | Common misconception | Precise definition |
|---|---|---|
| Lease | "A secret with a reminder to rotate it." | The server's record that a credential exists: an ID, a TTL, a maximum TTL, the token that requested it and the statements that revoke it. Expiry or revocation runs those statements; renewal moves the deadline. |
| Dynamic secret | "A password the server rotates for you." | A credential created at request time for one client, such as a PostgreSQL user made by `database/creds/ROLE`, that exists only for the life of its lease. |
| Envelope encryption | "Encrypting the data twice." | Encrypting each value under its own data key and storing that key wrapped by a key-encryption key, so rotating or revoking the outer key touches 60 bytes per value instead of the data. |
| AEAD | "Encryption that also hashes the data." | Authenticated encryption with associated data: a cipher plus a MAC over the ciphertext and extra context bytes, which fails closed on any change, a wrong key or the wrong context. |
| Shamir share | "A piece of the key: two of five shares give 40% of it." | One point on a random polynomial per secret byte over GF(2^8), plus its x coordinate. Below the threshold the shares give no information; at the threshold they give the whole key. |
| Seal | "Encrypting the disk." | The state in which the server holds only ciphertext and refuses requests until enough shares (or the KMS) rebuild the unseal key in memory. |
| Root token | "The admin password." | A token with the `root` policy and no expiry that bypasses every ACL check; create one for a task with a quorum of unseal shares and revoke it afterwards. |
| Orphan token | "A token whose owner left." | A token without a parent, so revoking the token that created it does not revoke it. Created with `create-orphan`, `no_parent` or by `revoke -mode=orphan` on its parent. |
| Path rule | "Every matching rule adds its capabilities." | One `path` stanza. For a request only the most specific matching rule counts; rules for the same path in different policies are merged, and `deny` on that rule wins. |
| Check-and-set | "A lock on the secret." | A write condition: `options.cas` must equal the current version or the write is refused, so a writer that read version 3 cannot overwrite version 4. |
| Audit HMAC | "The log is encrypted." | Each string value in the log replaced by HMAC-SHA256 under the audit device's key: equal values give equal hashes, so you can search for a known value but cannot read one. |

## Further Reading

- [DevSecOps Fundamentals](https://cloudcaptain.io/docs/tools/devsecops/fundamentals): where secrets management sits next to scanning, signing and policy in a pipeline.
- [Kubernetes Security](https://cloudcaptain.io/docs/tools/kubernetes/security): RBAC on Secrets, encryption at rest and the admission controls around them.
- [Cloud Security Fundamentals](https://cloudcaptain.io/docs/cloud/cloud-security/fundamentals): cloud KMS, secret managers and the shared-responsibility line for keys.
- [AWS Security and IAM](https://cloudcaptain.io/docs/cloud/aws/security-iam): Secrets Manager, KMS and the IAM conditions that limit who can decrypt.
- [RFC 8439: ChaCha20 and Poly1305 for IETF Protocols](https://www.rfc-editor.org/rfc/rfc8439): the algorithms of Steps 1 and 2 and every test vector the tests use.
- [How to Share a Secret (Shamir, 1979)](https://dl.acm.org/doi/10.1145/359168.359176): the two-page paper behind Step 4.
- [OpenBao: KV secrets engine version 2](https://openbao.org/docs/secrets/kv/kv-v2/): versions, soft delete, destroy, check-and-set and metadata.
- [OpenBao: policies](https://openbao.org/docs/concepts/policies/): the priority rules for globs and `+`, capabilities and parameter constraints.
- [OpenBao: tokens](https://openbao.org/docs/concepts/tokens/) and [leases](https://openbao.org/docs/concepts/lease/): TTLs, the token tree, orphans, renewal and revocation.
- [OpenBao: seal and unseal](https://openbao.org/docs/concepts/seal/): Shamir shares, the root key and keyring, auto-unseal.
- [OpenBao: PostgreSQL database secrets engine](https://openbao.org/docs/secrets/databases/postgresql/): connection settings, statement templates and root rotation.
- [OpenBao: declarative audit devices](https://openbao.org/docs/configuration/audit/): the `audit` stanza the Use It server configures.
- [Kubernetes: good practices for Secrets](https://kubernetes.io/docs/concepts/security/secrets-good-practices/) and [encrypting data at rest](https://kubernetes.io/docs/tasks/administer-cluster/encrypt-data/): what a Secret does and does not protect, and the KMS v2 provider.
- [NIST SP 800-38D](https://csrc.nist.gov/pubs/sp/800/38/d/final): the nonce rules for GCM, including the limit for random 96-bit nonces under one key, which apply in the same way to ChaCha20-Poly1305.
