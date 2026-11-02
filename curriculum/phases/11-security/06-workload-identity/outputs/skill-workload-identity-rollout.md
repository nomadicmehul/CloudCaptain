---
name: workload-identity-rollout
description: Move one service from network allowlists and shared keys to SPIFFE identities and mutual TLS, by inventorying its callers, fixing the ID scheme, writing precise registration entries, delivering short-lived SVIDs, and enforcing authorization by SPIFFE ID only after an audit period
version: 1.0.0
phase: security
lesson: security/workload-identity
tags: [spiffe, spire, mtls, zero-trust, workload-identity, x509, service-mesh, authorization, rotation]
---

# Workload identity rollout

Use this when a service authenticates callers by source IP, a shared API key, a long-lived client certificate or "it is inside the VPC", or when someone asks to "turn on mTLS". The goal is not encryption: it is that every connection to the service carries a short-lived identity the platform issued after attestation, and that the service allows callers by that identity. Do not enforce anything until step 6; every earlier step is observable and reversible.

## 1. Inventory who calls the service

```bash
SVC=payments-api; NS=payments
kubectl -n $NS get svc,deploy,sa -o wide
kubectl -n $NS logs deploy/$SVC --since=24h | grep -oE 'remote_addr=[0-9.]+' | sort | uniq -c | sort -rn | head -20
kubectl get pods -A -o wide | grep -F -f <(kubectl -n $NS logs deploy/$SVC --since=24h | grep -oE 'remote_addr=[0-9.]+' | cut -d= -f2 | sort -u)
```

Write a table: caller workload, namespace, ServiceAccount (or host user), endpoints it uses, and how it authenticates today. A caller you cannot name from its address is the reason for this rollout; put it on the list as "unknown" and find it before step 6.

## 2. Fix the trust domain and the ID scheme

| Decision | Recommendation |
|---|---|
| Trust domain | one per security boundary (`prod.example.com`, `staging.example.com`), never shared between production and anything else; a name you own, so federation partners cannot collide with it |
| Path scheme | derived from what the platform attests: `/ns/NAMESPACE/sa/SERVICE_ACCOUNT` on Kubernetes, `/host/HOSTNAME/svc/NAME` for VMs; never from what the workload claims |
| One ServiceAccount per workload | required: if five Deployments share `default`, they share one identity and the service cannot tell them apart |
| SVID lifetime | 1 hour (SPIRE's default); shorter only if renewal infrastructure is proven; never days |
| Key type | EC P-256 unless a peer cannot handle it |

Check every proposed ID with `python3 miniid.py id spiffe://...` (or go-spiffe): no uppercase trust domain, no trailing slash, no empty or `..` segments.

## 3. Register entries with selectors that match only the intended workload

```bash
S=/run/spire/server/api.sock
spire-server entry create -socketPath $S -parentID spiffe://prod.example.com/spire/agent/k8s_psat/CLUSTER/NODE_UID \
  -spiffeID spiffe://prod.example.com/ns/payments/sa/api -selector k8s:ns:payments -selector k8s:sa:api -x509SVIDTTL 3600
spire-server entry show -socketPath $S -spiffeID spiffe://prod.example.com/ns/payments/sa/api
```

On Kubernetes prefer a `ClusterSPIFFEID` (SPIRE Controller Manager) with the template `spiffe://{{ .TrustDomain }}/ns/{{ .PodMeta.Namespace }}/sa/{{ .PodSpec.ServiceAccountName }}` over hand-written entries.

| Selector set | Verdict |
|---|---|
| `k8s:ns:X` alone | too broad: every pod in the namespace gets the identity |
| `k8s:ns:X` + `k8s:sa:Y` | the normal choice |
| `k8s:pod-label:app:Y` alone | weak: anyone who can create a pod with that label gets it |
| `unix:uid:N` | fine on a VM where the UID is dedicated to the service; add `unix:path:` (with `discover_workload_path`) if other binaries run as that user |
| `unix:user:root` / `unix:uid:0` | almost never; root can attest as anything on that host anyway |

All of an entry's selectors must match, so adding selectors narrows an entry. Then confirm from inside the workload that it gets exactly one identity:

```bash
kubectl -n $NS exec deploy/$SVC -- /opt/spire/bin/spire-agent api fetch x509 -socketPath /spiffe-workload-api/spire-agent.sock -write /tmp
kubectl -n $NS exec deploy/$SVC -- cat /tmp/svid.0.pem | openssl x509 -noout -ext subjectAltName -dates
```

`PermissionDenied ... no identity issued` means no entry's selectors are all present: compare `entry show` with what the agent discovered (agent debug logs list the selectors for the PID).

## 4. Deliver the SVID to the application

| Application | Delivery |
|---|---|
| Go, Java, Rust, Python with a SPIFFE library | go-spiffe `workloadapi.NewX509Source`, java-spiffe; certificates rotate in memory |
| Reads certificates from files (nginx, PostgreSQL, JVM keystores) | `spiffe-helper` in daemon mode writing `cert_dir` and signalling the process (`renew_signal`, `pid_file_name`) |
| Cannot do TLS, or many services at once | a mesh (Istio ambient ztunnel, Linkerd) or a sidecar proxy (Envoy with SDS, ghostunnel) |
| Pods without SPIRE | cert-manager csi-driver-spiffe volume |

Verify rotation before going further: watch the certificate change at about half of its lifetime.

```bash
spire-agent api watch -socketPath /run/spire/agent/public/api.sock
kubectl -n $NS exec deploy/$SVC -- sh -c 'openssl x509 -in /etc/svid/svid.pem -noout -serial -enddate'
```

## 5. Authorization in audit mode

Turn on mutual TLS with the new identities but log instead of deny: an Istio `AuthorizationPolicy` with the annotation `istio.io/dry-run: "true"` (the proxy logs what it would have decided), Linkerd with the default policy still `all-unauthenticated` while you read the client identities from `linkerd viz edges` and `linkerd viz tap`, or the application logging the peer's SPIFFE ID next to every request. Run for at least one full business cycle (a week, including batch jobs and month-end runs). Build the allow list from the logs:

| Peer seen | Action |
|---|---|
| a SPIFFE ID from the inventory | add the exact ID to the allow list |
| a SPIFFE ID not in the inventory | find the owner; add or block deliberately |
| no client certificate (plain TLS or plaintext) | a caller not yet onboarded: back to step 3 for it |
| an ID from a federated trust domain | allow only exact IDs, never the whole foreign trust domain |
| `report-batch`-style callers with no business reason | do not allow; that is the finding of the rollout |

## 6. Enforce

Switch the policy to deny by default with the allow list from step 5 (Istio `ALLOW` policy with `principals: ["prod.example.com/ns/web/sa/frontend"]`, Linkerd `MeshTLSAuthentication` plus `AuthorizationPolicy`, or the library's `tlsconfig.AuthorizeOneOf`). Then remove what it replaces: the shared API key, the IP allowlist, the long-lived certificate Secret. Test both directions:

```bash
kubectl -n web exec deploy/frontend -- curl -sS -o /dev/null -w '%{http_code}\n' http://payments-api.payments:8080/health   # 200
kubectl -n batch exec deploy/report-batch -- curl -sS -o /dev/null -w '%{http_code}\n' http://payments-api.payments:8080/health   # 403 or reset
```

## 7. Operate

| Signal | Alert when |
|---|---|
| SVID remaining lifetime on any workload | below 25% (rotation at 50% has failed) |
| SPIRE server CA and upstream CA expiry | under 7 days; CA rotation is automatic only if the server is healthy |
| agent health, `spire-agent healthcheck` | failing on any node |
| authorization denials per caller ID | a new caller ID, or a spike from a known one |
| connections older than the SVID lifetime | if long-lived connections matter, cap connection age (`max_connection_duration`) |

## Report

State: the service and its trust domain; the caller inventory as a table (workload, SPIFFE ID, selectors, delivery method); the SVID TTL and the rotation evidence (two serials and times); the audit period and what it found, including callers that were blocked on purpose; the final allow list; the mechanisms removed (key, IP allowlist, Secret); the denial test results from both an allowed and a disallowed caller; and the alerts in place. Never report "mTLS enabled" without the allow list: encryption without authorization by identity is not the result of this runbook.
