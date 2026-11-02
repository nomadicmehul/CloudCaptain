# Networking

*Build a resolver, a TCP proxy and a TLS handshake before you configure nginx or Envoy.*

Most outages that look like application bugs are network questions with a precise answer: which route the kernel picked, which name the resolver really asked for, which side of a connection stopped reading, which certificate in the chain failed, which header made a cache keep a stale page. The tools that answer them (`ip route get`, `dig`, `ss`, `tcpdump`, `openssl s_client`, `curl -v`) print exactly what happened, but only if you know the mechanism behind each line. This phase builds each mechanism as a small tool from the standard library first and then reads the real tool's output against it, on a Linux host with network namespaces, real servers and real packets.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| [Routing](01-ip-routing/docs/en.md) | Build | `routelens`: CIDR arithmetic, `ip -j` parsing and a from-scratch `ip route get` with policy rules, longest-prefix match, metrics, source selection and ECMP | `ip route get`, `ip rule`, `ip neigh`, network namespaces, `tracepath` |
| [DNS](02-dns-resolver/docs/en.md) | Build | `minidig`: the RFC 1035 wire format with compression, an iterative resolver from root hints, TCP fallback, EDNS0, a TTL and negative cache, and `search`/`ndots` expansion | `dig +trace`, `dig +norecurse`, `resolvectl`, CoreDNS, the `ndots:5` cost |
| [TCP states and timeouts](03-tcp-states/docs/en.md) | Build | `tcpwatch`: `/proc/net/tcp` and `ss` parsing, the TCP state machine, CLOSE_WAIT leaks, listen-queue overflow, port exhaustion, and clients with connect, read and keepalive timeouts | `ss -tanpi`, `nstat`, `sysctl net.ipv4.tcp_*`, `curl --connect-timeout` |
| [Packet capture](04-packet-capture/docs/en.md) | Build | `pcaplens`: pcap and pcapng readers, Ethernet, cooked, IP, TCP and UDP decoding, a filter language subset and per-connection stats (RTT, retransmissions, RSTs) | `tcpdump -w/-r/-d`, capture in a pod's namespace, Wireshark |
| [TLS](05-tls-handshake/docs/en.md) | Build | `tlslens`: ClientHello and ServerHello parsing, an X.509 DER reader, chain building and RSA signature checks with `pow()`, hostname and expiry rules with OpenSSL-style errors | `openssl s_client`, `openssl verify`, `openssl x509`, `curl -v`, cert-manager |
| [HTTP/1.1](06-http-server/docs/en.md) | Build | `minihttp`: a server on raw sockets with keep-alive, chunked bodies, the smuggling rule, slowloris timeouts, ranges and ETags, and a reverse-proxy mode | `curl -v`, nginx `proxy_pass` and timeouts, what HTTP/2 and HTTP/3 change |
| [Load balancing](07-load-balancing/docs/en.md) | Build | `minilb`: round robin, least connections, power of two choices, ring and Maglev hashing, active and passive health checks, retry budgets and draining | nginx `upstream`, HAProxy `balance` and `check`, Envoy outlier detection |
| [HTTP caching](08-http-caching/docs/en.md) | Build | `minicache`: an RFC 9111 caching proxy with `Vary` keys, freshness, revalidation, stale-while-revalidate, stale-if-error, request collapsing and purge | `curl -I`, nginx `proxy_cache`, CDN cache headers |

Further lessons for this phase are proposed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md).

## Reference layer

The long-form guides, cheat sheet and interview questions for this phase live under [Networking](https://cloudcaptain.io/docs/tools/networking) and [nginx](https://cloudcaptain.io/docs/tools/nginx). Keep the networking cheat sheet open while you work.
