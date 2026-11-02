---
name: tls-certificate-triage
description: Find out why a TLS connection fails by separating handshake failures from chain failures, capturing the chain the server really sends with and without SNI, mapping the OpenSSL verify code to one confirming command and one fix, and checking expiry and renewal, without ever disabling verification
version: 1.0.0
phase: networking
lesson: networking/tls-handshake
tags: [tls, x509, certificates, openssl, curl, sni, alpn, cert-manager, acme, debugging]
---

# TLS certificate triage

Use this when a client reports `certificate verify failed`, `PKIX path building failed`, `x509: certificate signed by unknown authority`, curl exit 60 or 35, a browser certificate warning, or "it works in the browser but not in the script". Do not add `-k`, `--insecure`, `verify=False` or `InsecureSkipVerify` at any point, not even "to test": it removes the evidence and the protection together.

Set the target once:

```bash
HOST=app.example.com; PORT=443; ADDR=$HOST     # ADDR may be an IP or a load balancer name; HOST is the name the client asks for
```

## 1. Handshake failure or chain failure?

```bash
openssl s_client -connect $ADDR:$PORT -servername $HOST -verify_hostname $HOST -brief </dev/null 2>&1 | head -20
curl -sv -o /dev/null https://$HOST:$PORT/ 2>&1 | grep -E 'ALPN|SSL connection|SSL certificate|alert|error'; echo "curl exit ${PIPESTATUS[0]}"
```

| What you see | Meaning | Next |
|---|---|---|
| `Connection refused`, timeout, no `CONNECTED` | not TLS yet: routing, firewall, nothing listening | the TCP lesson's runbook, `ss -lnt` on the server |
| `wrong version number`, `packet length too long`, `http request` | plaintext on the TLS port, or TLS on a plaintext port | check which port terminates TLS; `tlslens hello` names plaintext HTTP |
| `alert handshake failure` (40), `protocol version` (70), `no protocols available`, curl exit 35 | client and server share no version, cipher, group or signature algorithm | step 2 |
| `alert unrecognized_name` (112) | the server has no certificate for that SNI | step 3, compare with and without `-servername` |
| `alert no_application_protocol` (120) | no common ALPN value | compare `-alpn h2,http/1.1` with the server's ALPN config |
| `verify error:num=N` / curl exit 60 / `SSLCertVerificationError` | the handshake worked, the chain did not verify | step 3, then step 4 with code N |
| `alert bad_certificate` (42), `certificate_required` (116), `unknown_ca` (48) sent by the server | the server rejected the client's certificate (mutual TLS) | check the client cert, key and the CA the server trusts |

## 2. Handshake: what each side offers

```bash
python3 tlslens.py listen --port 8443 --out hello.bin &        # point the failing client at 127.0.0.1:8443 (or --resolve)
python3 tlslens.py hello hello.bin                              # versions, suites, groups, signature algorithms, SNI, ALPN
for v in -tls1_2 -tls1_3; do echo "$v"; openssl s_client -connect $ADDR:$PORT -servername $HOST $v -brief </dev/null 2>&1 | head -3; done
python3 tlslens.py probe $ADDR:$PORT --servername $HOST          # TLS 1.2 ServerHello, chosen suite, downgrade sentinel
```

| Finding | Fix |
|---|---|
| client offers max TLS 1.0/1.1 | upgrade the client runtime; do not re-enable old versions on the server |
| server TLS 1.3 only, client TLS 1.2 only | enable TLS 1.2 on the server for that listener, or upgrade the client |
| no shared cipher suite (old client, or a FIPS-restricted server) | align the suite lists; prefer ECDHE with AES-GCM or ChaCha20 |
| no shared signature algorithm (an ECDSA-only cert and a client without `ecdsa_secp256r1_sha256`) | serve an RSA certificate as well, or upgrade the client |
| client sends no SNI | the client is old or connects by IP; it gets the default certificate; set SNI in the client or make the default certificate cover that name |

## 3. Collect the chain the server really sends

```bash
openssl s_client -connect $ADDR:$PORT -servername $HOST -showcerts </dev/null > with-sni.txt 2>&1
openssl s_client -connect $ADDR:$PORT -showcerts </dev/null > without-sni.txt 2>&1
grep -E '^ *[0-9]+ s:|^ *i:' with-sni.txt                       # subject and issuer of every certificate sent, leaf first
python3 tlslens.py verify with-sni.txt --host $HOST              # chain, first error with code and depth, warnings
python3 tlslens.py x509 with-sni.txt                             # SANs, validity, key ids, AIA caIssuers URL
```

Write down: how many certificates were sent, whether each certificate's issuer is the subject of the next one, whether the last one is a root (it need not be sent), and whether the leaf without SNI differs from the leaf with SNI.

## 4. Map the code to one check and one fix

| Code and text | Confirm with | Fix |
|---|---|---|
| 20 unable to get local issuer certificate (depth 0) | only one certificate in `with-sni.txt`; the leaf's `caIssuers` URL | serve `fullchain.pem` (leaf then intermediates), not `cert.pem`; on a load balancer upload the chain field |
| 20 at depth 1 or more | the top certificate sent is an intermediate whose root is not in the client's store | update the client's CA bundle (`ca-certificates`, the JDK `cacerts`, the container base image) or use a chain to a root the clients have |
| 2 unable to get issuer certificate | an intermediate is in the trust store but its root is not | add the root, or accept partial chains deliberately (`-partial_chain`, Python 3.13 default) |
| 19 self-signed certificate in certificate chain | the top certificate signs itself and is not trusted; same result for every site means an intercepting proxy | add the organisation's root to the client trust store, or fix the server to chain to a public root |
| 18 self-signed certificate | a single self-signed leaf | issue from a CA; for internal services trust the private CA, not the leaf |
| 10 certificate has expired | `openssl x509 -noout -enddate`, and which depth expired | renew; if an intermediate or cross-sign expired, serve the current chain; find out why automation did not renew (step 5) |
| 9 certificate is not yet valid | `date -u` on the client against `notBefore` | fix the client clock (NTP), or wait out a certificate issued with a future start |
| 62 hostname mismatch | `openssl x509 -noout -ext subjectAltName` against `$HOST` | reissue with the name in the SAN; remember `*.a.com` covers one label and not `a.com`; the CN is ignored |
| 64 IP address mismatch | the client connects by IP | connect by name, or add an `IP:` SAN for internal endpoints |
| 26 unsuitable certificate purpose | `extendedKeyUsage` lacks `serverAuth` (often a client certificate reused on a server) | reissue with `serverAuth` |
| 79 invalid CA certificate / 32 key usage does not include certificate signing | an issuer without `CA:TRUE` or `keyCertSign` | the wrong certificate is in the chain position; rebuild the chain from the CA's bundle |
| 25 path length constraint exceeded | an intermediate with `pathlen:0` above another intermediate | use the issuing CA directly below it, or a CA whose constraints allow the depth |
| 7 certificate signature failure | a corrupted or hand-edited certificate, or the wrong issuer with the same name | re-download the chain from the CA |
| 23 certificate revoked | CRL or OCSP status | reissue; find out why it was revoked before deploying the new one |

## 5. Expiry and renewal

```bash
openssl s_client -connect $ADDR:$PORT -servername $HOST </dev/null 2>/dev/null | openssl x509 -noout -enddate -checkend 1209600 || echo "expires within 14 days"
kubectl get certificate -A 2>/dev/null                                     # cert-manager: READY and the secret name
kubectl -n NS describe certificate NAME 2>/dev/null | sed -n '/Status:/,$p'  # renewal time, failing Order or Challenge
```

| Situation | Verdict |
|---|---|
| expires within 14 days, automation present | find the failing challenge (HTTP-01 path not reachable, DNS-01 credentials, rate limits) before it expires |
| renewed Secret but old certificate still served | the server did not reload: restart or reload the proxy, check the ingress or gateway controller picked up the Secret |
| no automation | schedule it now; public certificate lifetimes are shrinking in steps to 47 days |

## 6. Verify

```bash
openssl s_client -connect $ADDR:$PORT -servername $HOST -verify_hostname $HOST -verify_return_error </dev/null 2>&1 | grep 'Verify return code'
curl -s -o /dev/null -w '%{ssl_verify_result} %{http_code}\n' https://$HOST:$PORT/
```

Expect `Verify return code: 0 (ok)` and `0` from curl, from the failing client's network and with its trust store (inside its container image, if it runs in one).

## 7. Report

State: the client, its TLS library and trust store; handshake or chain failure; the exact error with code and depth; the chain as sent (count, subjects, issuers) with and without SNI; the confirming command output; the change made (chain file, SAN, trust store, clock, renewal); the verification result afterwards from the client's side; and any client where verification had been disabled during the incident, so it can be turned back on. Never list "added -k" or "disabled verification" as a resolution.
