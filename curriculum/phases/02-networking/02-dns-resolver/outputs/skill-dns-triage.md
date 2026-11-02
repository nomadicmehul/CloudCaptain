---
name: dns-triage
description: Find out why a name does not resolve, resolves to an old address or resolves slowly, by checking what the application really asks, asking each resolver and authoritative server directly, walking the delegation, and computing how long a cached or negative answer will live
version: 1.0.0
phase: networking
lesson: networking/dns-resolver
tags: [dns, dig, resolv.conf, ndots, coredns, kubernetes, caching, ttl, nxdomain, edns, tcp]
---

# DNS triage

Use this when an application reports "could not resolve host", `NXDOMAIN`, `SERVFAIL` or `EAI_AGAIN`, when a changed record still returns the old address, when lookups are slow, or when someone proposes "restart CoreDNS" or "flush the cache" as the fix. Do not restart or flush anything during triage: a cache holds the evidence (what was cached, and with how many seconds left), and a restart only moves the problem to the next cache in the chain.

Set the variables once:

```bash
NAME=<the name exactly as the application uses it>     # keep or omit the trailing dot exactly as configured
TYPE=A                                                 # AAAA, CNAME, MX, SRV, TXT as needed
```

## 1. What does the application actually ask for?

`dig` talks to a server and never reads `/etc/hosts`, `nsswitch.conf` or the search list the way an application does. Start from the application's view:

```bash
getent ahosts "$NAME"; echo "exit $?"                 # the getaddrinfo() path: nsswitch, /etc/hosts, then DNS
grep '^hosts:' /etc/nsswitch.conf
cat /etc/resolv.conf                                   # nameserver, search, options (ndots, timeout, attempts, edns0)
grep -i "$NAME" /etc/hosts
python3 minidig.py search "$NAME" --resolv-conf /etc/resolv.conf    # every candidate name, in order
```

In a pod, run the same through `kubectl exec POD -- cat /etc/resolv.conf` (or an ephemeral debug container) and remember that the pod's resolver is the `kube-dns` Service IP, not the node's.

| What you see | Meaning | Next |
|---|---|---|
| `getent` finds it, the app does not | the app has its own resolver (Go's pure resolver, Java's cache, a DNS client library) or caches failures itself | check the runtime's DNS settings and its negative cache (`networkaddress.cache.negative.ttl` in Java) |
| `getent` returns an address from `/etc/hosts` | DNS is never asked | fix or remove the hosts entry |
| the name has fewer dots than `ndots` | every search suffix is tried first | step 6 for the cost; a wrong answer from a search suffix (a wildcard in a search domain) is possible |
| `nameserver 127.0.0.53` | systemd-resolved's stub | `resolvectl status` shows the real upstream per link; `resolvectl query "$NAME"` |
| `getent` fails, `dig "$NAME"` works | the candidates differ (search, `ndots`, trailing dot) or nsswitch stops before `dns` (`[NOTFOUND=return]`) | compare `minidig search` output with the name `dig` used |

## 2. Ask the configured resolver, then the authoritative servers

```bash
NS1=$(awk '/^nameserver/ {print $2; exit}' /etc/resolv.conf)
dig @"$NS1" "$NAME" "$TYPE"                            # what the resolver your host uses says, and from cache or not
dig +short NS "$(echo "$NAME" | cut -d. -f2-)"         # the zone's name servers (walk up a label if empty)
dig +norecurse @<an authoritative server> "$NAME" "$TYPE"   # the source of truth: aa flag, zone TTL
```

Read the flags line first. `aa` means an authoritative server answered; an answer without `aa` came from a cache. `status:` is the RCODE. Compare the TTL from the resolver with the TTL from the authoritative server: the resolver's is the time left in its cache.

| Resolver says | Authoritative says | Meaning |
|---|---|---|
| old address, TTL counting down | new address | a cached copy; it lives for the remaining TTL. Lowering the TTL now does not shorten copies already cached |
| `NXDOMAIN`, SOA TTL counting down | the record exists | negative caching: by RFC 2308 it lives for min(SOA TTL, SOA MINIMUM) from the first failed lookup; a cache that uses the SOA's TTL as sent (CoreDNS `cache`) keeps it for that TTL up to its cap. The SOA TTL in the resolver's answer is the time left |
| `SERVFAIL` | answers fine | the resolver cannot complete the walk: step 3 (delegation), step 4 (transport) or DNSSEC validation (`dig +cd` succeeds when validation is the cause) |
| `REFUSED` | answers fine | the resolver does not serve your client (ACL) or that zone; check which server you are really asking |
| the same answer | the same answer | DNS is fine; the problem is in step 1 (what the app asks) or not DNS at all |

## 3. Walk the delegation

```bash
dig -4 +trace "$NAME" "$TYPE"                                  # one block per zone cut
python3 minidig.py resolve "$NAME" --type "$TYPE" --trace     # the same walk, with referral and glue per step
dig +norecurse @<parent server> "$(echo "$NAME" | cut -d. -f2-)" NS    # what the parent delegates
dig +norecurse @<child server> "$(echo "$NAME" | cut -d. -f2-)" NS     # what the child claims
```

| What the walk shows | Cause | Fix |
|---|---|---|
| a server answers a referral back up, or `REFUSED`, for a zone it is listed for | lame delegation | fix the NS records at the parent (registrar or parent zone) or configure the zone on that server |
| NS names inside the child zone and no glue in the referral | missing glue | add glue at the parent (registrar "host records") |
| parent and child list different NS sets | stale delegation after a provider move | update the parent; resolvers trust the parent's NS until they reach the child |
| one name server times out, others answer | partial outage | resolvers retry elsewhere but get slower; remove or fix the dead server |
| CNAME chain that loops or goes deeper than a handful of hops | broken alias | resolvers stop with `SERVFAIL`; flatten the chain |

## 4. Transport: size, truncation and TCP

```bash
dig +noedns +ignore @"$NS1" "$NAME" "$TYPE" | grep flags:     # tc set: the answer needs more than 512 bytes
dig +tcp @"$NS1" "$NAME" "$TYPE"                              # fails while UDP works: TCP 53 is blocked
dig +bufsize=1232 @"$NS1" "$NAME" "$TYPE"; dig +bufsize=4096 @"$NS1" "$NAME" "$TYPE"   # large buffers can fragment and vanish
```

Large answers (many A records, long TXT, DNSSEC) need EDNS or TCP. A firewall or security group that allows UDP 53 but not TCP 53 breaks only those names, which looks random. Fragmented UDP responses above about 1232 bytes are often dropped on the path; resolvers that advertise 1232 fall back to TCP instead.

## 5. How long will it stay wrong?

| Situation | It lasts | Report it as |
|---|---|---|
| stale positive answer | the TTL left in the resolver (`dig @resolver` shows it) | "until HH:MM, when the cached copy expires" |
| cached `NXDOMAIN` or NODATA | min(SOA TTL, SOA MINIMUM) from the first failure (or the SOA's own TTL, capped, in caches that ignore MINIMUM), often 300 to 3600 s | "created after a failed lookup; negative cache until HH:MM"; keep the zone's SOA TTL at or below MINIMUM so every cache agrees |
| application or runtime cache | the runtime's own setting | name the setting |

Flushing is a targeted fix only when you control the cache and the authoritative data is already right: `resolvectl flush-caches` on a systemd host, or a rolling restart of the forwarder that holds the entry. It does not reach resolvers you do not control.

## 6. Kubernetes specifics

```bash
kubectl -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}'
kubectl -n kube-system get pods -l k8s-app=kube-dns -o wide
kubectl -n kube-system logs -l k8s-app=kube-dns --tail=50          # errors; add the log plugin briefly to see every query
kubectl get pod POD -o jsonpath='{.spec.dnsPolicy} {.spec.dnsConfig}{"\n"}'
```

| Symptom | Cause | Fix |
|---|---|---|
| external names slow, CoreDNS busy with `*.svc.cluster.local` NXDOMAINs | `ndots:5` and the search list: up to five names, ten packets with AAAA | trailing dot in config, `dnsConfig.options` `ndots: "2"`, NodeLocal DNSCache, or the `autopath` plugin |
| Service names fail, external names work | `kubernetes` plugin or API problems, or the wrong namespace in the search list | `kubectl get svc,endpointslices -n NS`; use `svc.ns.svc.cluster.local` |
| external names fail, Service names work | `forward . /etc/resolv.conf` points at a node resolver that is broken or unreachable | check the node's `/etc/resolv.conf` and the upstream from a node |
| `dnsPolicy: Default` pod cannot resolve Services | it uses the node's resolver, not CoreDNS | use `ClusterFirst` (the default) |

## 7. Report

State: the name exactly as the application used it and the candidate names it expanded to; the answer from the configured resolver (flags, RCODE, TTL) and from an authoritative server; where the walk broke, if it did; whether truncation or TCP was involved; how long the wrong answer will live and why (TTL or SOA minimum, with the numbers); the change you made or recommend and who owns it (zone owner, registrar, cluster DNS, application). Never list "restarted CoreDNS" or "flushed the cache" as a root cause.
