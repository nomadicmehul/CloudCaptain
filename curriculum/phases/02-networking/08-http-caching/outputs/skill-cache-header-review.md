---
name: cache-header-review
description: Review a site's HTTP caching headers per URL class (HTML, hashed assets, static files, APIs, personalised pages, errors) for browsers, shared caches, CDNs and nginx; find leaks, misses and stale-content traps, recommend headers, verify hits and 304s, and plan purges
version: 1.0.0
phase: networking
lesson: networking/http-caching
tags: [http, caching, cdn, nginx, cache-control, etag, vary, rfc9111]
---

# Cache header review

Use this before a CDN or a caching proxy goes in front of a site, after an incident where users saw stale or someone else's content, when a CDN's hit ratio is low, or when the origin falls over at the moment cached pages expire. The goal is one deliberate `Cache-Control` (and validator, and `Vary`) per class of URL, proven at the cache.

## 1. List the URL classes

Collect ten to twenty real URLs from access logs or a crawl and sort them into classes. Every class gets one policy; a URL that fits none is a finding.

| Class | Examples | Who may store it |
|---|---|---|
| HTML documents | `/`, `/products/42`, `/catalogue/` | every cache, revalidated |
| Hashed static assets | `/static/app.3f9a1c2b.js`, `/assets/index-BqM3xT7f.css` | every cache, for a year |
| Unhashed static files | `/img/logo.png`, `/favicon.ico`, `/robots.txt` | every cache, briefly, with a validator |
| Public API | `/api/products?page=2`, `/api/prices` | shared caches briefly, browsers briefly |
| Personalised responses | `/account`, `/api/cart`, anything that reads a session cookie or `Authorization` | the browser only, or nobody |
| Errors and redirects | 404 pages, 301/308 redirects, 5xx pages | 404 and 301 briefly; 5xx never |

## 2. Capture headers at every layer

For each URL, save the response headers as the client sees them and as the origin sends them, anonymous and logged in:

```bash
U=https://shop.example.com/products/42
curl -sv -o /dev/null "$U" 2> edge.txt                                             # through the CDN
curl -sv -o /dev/null --resolve shop.example.com:443:ORIGIN_IP "$U" 2> origin.txt   # straight to the origin
curl -sv -o /dev/null -H 'Cookie: session=TEST' "$U" 2> loggedin.txt
curl -s -D - -o /dev/null "$U" | grep -iE '^(age|cache-status|x-cache|cf-cache-status)'   # twice: the second should be a hit
```

Never paste a real session cookie into a ticket; use a test account.

## 3. Read each capture the way four caches will

```bash
python3 code/main.py check origin.txt --url "$U"           # browser, shared, cdn and nginx rows plus notes
python3 code/main.py check origin.txt --url "$U" --json > review-products-42.json
```

Without the lesson code, answer the same questions by hand from the headers: is it storable by a shared cache (`no-store`, `private`, `Set-Cookie`, `Authorization`, `Vary: *`), what is the lifetime (`s-maxage`, `max-age`, `Expires` minus `Date`, else a heuristic from `Last-Modified`), how much is left after `Age`, which validator renews it (`ETag`, `Last-Modified`, none), and may it be served stale (`stale-while-revalidate`, `stale-if-error`, forbidden by `must-revalidate`, `proxy-revalidate`, `s-maxage` or `no-cache`).

## 4. Compare with the recommended policy

| Class | Recommended headers | Why |
|---|---|---|
| HTML documents | `Cache-Control: no-cache` plus `ETag`; or `max-age=0, s-maxage=60, stale-while-revalidate=30, stale-if-error=600` behind a CDN you can purge | deploys show at once; each view costs a 304; the edge absorbs bursts and outages |
| Hashed static assets | `Cache-Control: public, max-age=31536000, immutable` | the name changes when the content does; no revalidation ever |
| Unhashed static files | `Cache-Control: public, max-age=3600` plus `ETag` or `Last-Modified` | short enough to pick up changes, cheap to renew |
| Public API | `Cache-Control: max-age=5, s-maxage=60, stale-while-revalidate=30, stale-if-error=300` plus `ETag`; `Vary` only on headers that change the body | browsers see fresh data, the edge collapses load |
| Personalised responses | `Cache-Control: private, no-cache` plus `ETag`; `no-store` for secrets (tokens, payment, health data) | never in a shared cache; `no-store` keeps it off disk too |
| 404 | `Cache-Control: max-age=60` | stops scanners hammering the origin, recovers quickly when the page appears |
| 301/308 | `Cache-Control: max-age=3600` until the redirect is known to be permanent | browsers keep permanent redirects for a long time |
| 5xx | `Cache-Control: no-store` | an error must not outlive the outage |

Targeted headers when a CDN is in front: `CDN-Cache-Control` for the edge lifetime (RFC 9213) next to a shorter `Cache-Control` for browsers, and `Surrogate-Key` or `Cache-Tag` on every response that a business event (a price change, an article edit) must purge.

## 5. Check the classic mistakes

| Finding | Severity | Fix |
|---|---|---|
| `Set-Cookie` on a response a shared cache may store | critical: one user's cookie replayed to others, or a permanent miss in nginx and most CDNs | `private`, or move the cookie to a separate request |
| Personalised content without `private` or `no-store` | critical | `private, no-cache` or `no-store` |
| `max-age` of a day or more on an unhashed URL | high: deploys reach users only after expiry | content hashes in file names, or a short `max-age` with a validator |
| `Vary: User-Agent`, `Vary: Cookie` | high: hit ratio near zero | drop it, or vary on a small normalised header the edge sets |
| No `ETag` and no `Last-Modified` | medium: every expiry is a full download | add a validator |
| `no-cache` where `no-store` was meant (or the reverse) | medium | `no-store` keeps it out of caches, `no-cache` only forces revalidation |
| No `stale-if-error` on pages that must survive origin outages | medium | `stale-if-error=600` or more; nginx also needs `proxy_cache_use_stale` for 5xx answers |
| No explicit lifetime, only `Last-Modified` | low: caches guess 10% of its age; nginx stores nothing | an explicit `max-age` |
| `Expires` next to `max-age`, `Pragma: no-cache` in responses | low: ignored by HTTP/1.1 caches | remove, or keep for HTTP/1.0 clients knowingly |
| `private` together with `s-maxage` | low: `s-maxage` has no effect | pick one |

## 6. Verify at the cache

```bash
for i in 1 2; do curl -s -D - -o /dev/null "$U" | grep -iE '^(age|cache-status|x-cache)'; done   # MISS then HIT, Age increasing
curl -s -D - -o /dev/null -H 'Cache-Control: no-cache' "$U" | grep -iE '^(cache-status|x-cache)'  # nginx ignores this unless configured
ETAG=$(curl -s -D - -o /dev/null "$U" | awk -F': ' 'tolower($1)=="etag"{print $2}' | tr -d '\r')
curl -s -o /dev/null -w '%{http_code}\n' -H "If-None-Match: $ETAG" "$U"                          # 304
```

On the origin, the access log should show 304s for revalidations and one request per key per lifetime. For nginx, log `$upstream_cache_status` and count each value per URL class.

## 7. Plan purges and deploys

Upload new hashed assets first, then the HTML that references them, then purge the HTML (by URL or by tag). Purge by tag for business events instead of listing URLs. Confirm that purge endpoints require authentication or are reachable only from the deployment network.

## 8. Report

One table per URL class: example URL, current `Cache-Control`/validator/`Vary`, what the browser, shared cache, CDN and nginx each do with it (stored or not, lifetime, stale allowance), the findings from step 5 with severity, the recommended headers, and the verification result from step 6 (hit on the second request, 304 on revalidation). Then the purge plan, the deploy order, and the expected change in origin request rate. Never include cookies, tokens or personal data from the captures.
