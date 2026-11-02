import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
T0 = 1_800_000_000.0
BASE = "http://cache.test"


def fixture(name):
    return os.path.join(FIXTURES, name)


def fixture_response(name):
    with open(fixture(name), encoding="utf-8") as handle:
        return main.parse_header_blocks(handle.read())[-1]


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeOrigin:
    def __init__(self, clock):
        self.clock = clock
        self.routes = {}
        self.calls = []
        self.failure = None
        self.delay = 0.0
        self.unsafe_reply = (200, [])
        self.lock = threading.Lock()

    def route(self, path, headers, body=b"body"):
        self.routes[path] = (200, list(headers), body)

    def __call__(self, method, url, headers, body):
        path = urllib.parse.urlsplit(url).path
        seen = {name.lower(): value for name, value in headers}
        with self.lock:
            self.calls.append((method, path, seen))
        if self.delay:
            time.sleep(self.delay)
        if self.failure in ("connect", "timeout"):
            raise main.UpstreamError(self.failure, "origin unreachable")
        if isinstance(self.failure, int):
            return main.Response(self.failure, [("Cache-Control", "no-store")], b"origin is down")
        if method not in ("GET", "HEAD"):
            status, extra = self.unsafe_reply
            return main.Response(status, [("Cache-Control", "no-store")] + extra, b"updated")
        status, route_headers, payload = self.routes[path]
        out = [("Date", main.http_date(self.clock()))] + route_headers
        etag = main.header(out, "ETag")
        if etag and seen.get("if-none-match") == etag:
            return main.Response(304, [h for h in out if h[0].lower() in ("date", "etag", "cache-control", "vary")], b"")
        return main.Response(status, out, payload + b" lang=" + seen.get("accept-language", "-").encode())

    def count(self, path):
        with self.lock:
            return sum(1 for _, seen_path, _ in self.calls if seen_path == path)

    def last(self, path):
        with self.lock:
            return [seen for _, seen_path, seen in self.calls if seen_path == path][-1]


def setup(policy=None, lock_timeout=5.0):
    clock = Clock()
    origin = FakeOrigin(clock)
    cache = main.Cache(origin, policy or main.Policy(), clock=clock, lock_timeout=lock_timeout)
    return cache, origin, clock


def get(cache, path, headers=None, method="GET"):
    return cache.handle(method, BASE + path, list(headers or []))


def h(result, name):
    return main.header(result.headers, name)


def entry_for(headers, response_time=T0, request_time=None, status=200):
    response = main.Response(status, [("Date", main.http_date(response_time))] + list(headers), b"x")
    return main.Entry("GET " + BASE + "/", BASE + "/", response, {}, response_time if request_time is None else request_time, response_time)


class ParsingTests(unittest.TestCase):
    def test_cache_control_is_case_insensitive_quoted_and_the_first_duplicate_wins(self):
        parsed = main.parse_cache_control('Public, Max-Age=60, no-cache="Set-Cookie, Set-Cookie2", max-age=5, s-maxage="30"')
        self.assertEqual(parsed, {"public": None, "max-age": "60", "no-cache": "Set-Cookie, Set-Cookie2", "s-maxage": "30"})
        self.assertEqual(main.delta_seconds("60"), 60)
        self.assertIsNone(main.delta_seconds("-1"))
        self.assertIsNone(main.delta_seconds("1e3"))
        self.assertEqual(main.delta_seconds("99999999999"), 2 ** 31)

    def test_http_dates_in_all_three_formats_and_invalid_ones(self):
        expected = 784111777.0
        for text in ("Sun, 06 Nov 1994 08:49:37 GMT", "Sunday, 06-Nov-94 08:49:37 GMT", "Sun Nov  6 08:49:37 1994"):
            self.assertEqual(main.parse_http_date(text), expected, text)
        for text in ("0", "", "-1", "tomorrow", None):
            self.assertIsNone(main.parse_http_date(text))

    def test_pragma_no_cache_counts_only_without_cache_control(self):
        self.assertEqual(main.request_directives([("Pragma", "no-cache")]), {"no-cache": None})
        self.assertEqual(main.request_directives([("Pragma", "no-cache"), ("Cache-Control", "max-age=5")]), {"max-age": "5"})


class FreshnessTests(unittest.TestCase):
    def test_lifetime_order_is_s_maxage_max_age_expires_then_heuristic(self):
        shared, private = main.Policy(), main.Policy(shared=False)
        both = [("Cache-Control", "s-maxage=30, max-age=10")]
        self.assertEqual(main.freshness_lifetime(200, both, shared, T0), (30.0, "s-maxage"))
        self.assertEqual(main.freshness_lifetime(200, both, private, T0), (10.0, "max-age"))
        expires = [("Date", main.http_date(T0)), ("Expires", main.http_date(T0 + 120)), ("Cache-Control", "public")]
        self.assertEqual(main.freshness_lifetime(200, expires, shared, T0), (120.0, "Expires - Date"))
        self.assertEqual(main.freshness_lifetime(200, [("Expires", "0")], shared, T0)[0], 0.0)
        old = [("Date", main.http_date(T0)), ("Last-Modified", main.http_date(T0 - 10 * 86400))]
        self.assertEqual(main.freshness_lifetime(200, old, shared, T0), (86400.0, "10% since Last-Modified"))
        self.assertEqual(main.freshness_lifetime(302, old, shared, T0), (0.0, "none"))
        self.assertEqual(main.freshness_lifetime(302, old + [("Cache-Control", "public")], shared, T0)[0], 86400.0)
        self.assertEqual(main.freshness_lifetime(200, [("Cache-Control", "max-age=soon")], shared, T0), (0.0, "max-age"))

    def test_current_age_follows_the_rfc_9111_formula(self):
        entry = entry_for([("Age", "3")], response_time=T0, request_time=T0 - 2)
        entry.response.headers = main.replace(entry.response.headers, "Date", main.http_date(T0 - 10))
        self.assertEqual(main.current_age(entry, T0 + 7), 17.0)
        upstream = entry_for([("Age", "30")], response_time=T0, request_time=T0 - 1)
        self.assertEqual(main.current_age(upstream, T0 + 4), 35.0)

    def test_storable_rules_for_shared_and_private_caches(self):
        shared, private = main.Policy(), main.Policy(shared=False)
        auth = [("Authorization", "Bearer t")]
        cases = [
            (shared, "GET", 200, [], [("Cache-Control", "no-store, max-age=60")], False),
            (shared, "GET", 200, [], [("Cache-Control", "private, max-age=60")], False),
            (private, "GET", 200, [], [("Cache-Control", "private, max-age=60")], True),
            (shared, "GET", 200, auth, [("Cache-Control", "max-age=60")], False),
            (shared, "GET", 200, auth, [("Cache-Control", "public, max-age=60")], True),
            (private, "GET", 200, auth, [("Cache-Control", "max-age=60")], True),
            (shared, "POST", 200, [], [("Cache-Control", "max-age=60")], False),
            (shared, "GET", 500, [], [], False),
            (shared, "GET", 500, [], [("Cache-Control", "max-age=5")], True),
            (shared, "GET", 404, [], [], True),
            (shared, "GET", 200, [], [("Cache-Control", "max-age=60"), ("Vary", "*")], False),
            (shared, "GET", 200, [], [("Cache-Control", "max-age=60"), ("Set-Cookie", "a=b")], False),
            (main.Policy(refuse_set_cookie=False), "GET", 200, [], [("Cache-Control", "max-age=60"), ("Set-Cookie", "a=b")], True),
            (shared, "GET", 200, [("Cache-Control", "no-store")], [("Cache-Control", "max-age=60")], False),
            (shared, "GET", 206, [], [("Cache-Control", "max-age=60")], False),
        ]
        for policy, method, status, request, response, want in cases:
            ok, why = main.storable(method, status, request, response, policy)
            self.assertEqual(ok, want, (policy.label, method, status, request, response, why))

    def test_assess_applies_request_and_response_directives(self):
        policy = main.Policy()
        fresh = entry_for([("Cache-Control", "max-age=60")])
        self.assertEqual(main.assess(fresh, [], T0 + 10, policy).action, "hit")
        verdict = main.assess(fresh, [("Cache-Control", "no-cache")], T0 + 10, policy)
        self.assertEqual((verdict.action, verdict.fwd), ("revalidate", "request"))
        self.assertEqual(main.assess(fresh, [("Cache-Control", "max-age=5")], T0 + 10, policy).action, "revalidate")
        self.assertEqual(main.assess(fresh, [("Cache-Control", "min-fresh=55")], T0 + 10, policy).action, "revalidate")
        self.assertEqual(main.assess(entry_for([("Cache-Control", "no-cache")]), [], T0, policy).action, "revalidate")
        self.assertEqual(main.assess(fresh, [], T0 + 70, policy).action, "revalidate")
        self.assertEqual(main.assess(fresh, [("Cache-Control", "max-stale")], T0 + 70, policy).action, "hit")
        self.assertEqual(main.assess(fresh, [("Cache-Control", "max-stale=5")], T0 + 70, policy).action, "revalidate")
        strict = entry_for([("Cache-Control", "max-age=60, must-revalidate")])
        self.assertEqual(main.assess(strict, [("Cache-Control", "max-stale")], T0 + 70, policy).action, "revalidate")
        edge = entry_for([("Cache-Control", "s-maxage=60")])
        self.assertEqual(main.assess(edge, [("Cache-Control", "max-stale")], T0 + 70, policy).action, "revalidate")
        swr = entry_for([("Cache-Control", "max-age=60, stale-while-revalidate=30")])
        self.assertEqual(main.assess(swr, [], T0 + 70, policy).action, "stale")
        self.assertEqual(main.assess(swr, [], T0 + 100, policy).action, "revalidate")


class CacheFlowTests(unittest.TestCase):
    def test_miss_then_hit_with_age_then_a_conditional_revalidation(self):
        cache, origin, clock = setup()
        origin.route("/page", [("Cache-Control", "max-age=60"), ("ETag", '"v1"')], b"hello")
        first = get(cache, "/page")
        self.assertEqual((first.status, first.cache_status), (200, "MISS"))
        self.assertIn("stored", h(first, "Cache-Status"))
        self.assertIsNone(h(first, "Age"))
        clock.advance(10)
        hit = get(cache, "/page")
        self.assertEqual((hit.cache_status, h(hit, "Age"), h(hit, "Cache-Status")), ("HIT", "10", "minicache; hit; ttl=50"))
        self.assertEqual(origin.count("/page"), 1)
        clock.advance(60)
        again = get(cache, "/page")
        self.assertEqual((again.status, again.cache_status, again.body), (200, "REVALIDATED", b"hello lang=-"))
        self.assertEqual(origin.last("/page").get("if-none-match"), '"v1"')
        self.assertIn("fwd=stale; fwd-status=304", h(again, "Cache-Status"))
        clock.advance(1)
        self.assertEqual((get(cache, "/page").cache_status, origin.count("/page")), ("HIT", 2))

    def test_a_full_response_on_revalidation_replaces_the_copy(self):
        cache, origin, clock = setup()
        origin.route("/page", [("Cache-Control", "max-age=60"), ("ETag", '"v1"')], b"old")
        get(cache, "/page")
        origin.route("/page", [("Cache-Control", "max-age=60"), ("ETag", '"v2"')], b"new")
        clock.advance(61)
        replaced = get(cache, "/page")
        self.assertEqual((replaced.cache_status, replaced.body), ("EXPIRED", b"new lang=-"))
        self.assertEqual(get(cache, "/page").body, b"new lang=-")
        self.assertEqual(len(cache.snapshot()), 1)

    def test_a_304_updates_the_stored_headers(self):
        cache, origin, clock = setup()
        origin.route("/page", [("Cache-Control", "max-age=10"), ("ETag", '"v1"')], b"same")
        get(cache, "/page")
        origin.route("/page", [("Cache-Control", "max-age=120"), ("ETag", '"v1"')], b"same")
        clock.advance(11)
        self.assertEqual(get(cache, "/page").cache_status, "REVALIDATED")
        clock.advance(100)
        hit = get(cache, "/page")
        self.assertEqual((hit.cache_status, h(hit, "Cache-Control")), ("HIT", "max-age=120"))

    def test_vary_stores_one_variant_per_normalised_header_value(self):
        cache, origin, _ = setup()
        origin.route("/vary", [("Cache-Control", "max-age=60"), ("Vary", "Accept-Language")], b"vary")
        english = get(cache, "/vary", [("Accept-Language", "en,  de")])
        german = get(cache, "/vary", [("Accept-Language", "de")])
        self.assertEqual((english.cache_status, german.cache_status), ("MISS", "MISS"))
        self.assertIn("fwd=vary-miss", h(german, "Cache-Status"))
        again = get(cache, "/vary", [("accept-language", "en, de")])
        self.assertEqual((again.cache_status, again.body), ("HIT", b"vary lang=en,  de"))
        self.assertEqual(get(cache, "/vary").cache_status, "MISS")
        self.assertEqual(origin.count("/vary"), 3)
        self.assertEqual(sorted(row["vary"]["accept-language"] or "" for row in cache.snapshot()), ["", "de", "en, de"])

    def test_no_store_and_private_are_not_stored_by_a_shared_cache_but_private_is_by_a_browser(self):
        cache, origin, _ = setup()
        origin.route("/nostore", [("Cache-Control", "no-store")])
        origin.route("/private", [("Cache-Control", "private, max-age=60")])
        for path in ("/nostore", "/nostore", "/private", "/private"):
            self.assertEqual(get(cache, path).cache_status, "MISS")
        self.assertEqual((origin.count("/nostore"), origin.count("/private")), (2, 2))
        self.assertIn('detail="no-store"', h(get(cache, "/nostore"), "Cache-Status"))
        browser, origin2, _ = setup(main.Policy(shared=False, refuse_set_cookie=False))
        origin2.route("/private", [("Cache-Control", "private, max-age=60")])
        self.assertEqual([get(browser, "/private").cache_status for _ in range(2)], ["MISS", "HIT"])

    def test_request_no_cache_revalidates_and_request_no_store_bypasses(self):
        cache, origin, _ = setup()
        origin.route("/page", [("Cache-Control", "max-age=60"), ("ETag", '"v1"')])
        get(cache, "/page")
        forced = get(cache, "/page", [("Cache-Control", "no-cache")])
        self.assertEqual(forced.cache_status, "REVALIDATED")
        self.assertIn("fwd=request", h(forced, "Cache-Status"))
        self.assertEqual(get(cache, "/page", [("Pragma", "no-cache")]).cache_status, "REVALIDATED")
        bypass = get(cache, "/page", [("Cache-Control", "no-store")])
        self.assertEqual((bypass.cache_status, origin.count("/page")), ("BYPASS", 4))
        self.assertIsNone(origin.last("/page").get("if-none-match"))

    def test_stale_if_error_serves_stale_on_5xx_and_on_connect_errors_inside_the_window(self):
        cache, origin, clock = setup()
        origin.route("/sie", [("Cache-Control", "max-age=10, stale-if-error=60"), ("ETag", '"v1"')], b"cached")
        get(cache, "/sie")
        clock.advance(20)
        origin.failure = 503
        stale = get(cache, "/sie")
        self.assertEqual((stale.status, stale.cache_status, stale.body), (200, "STALE", b"cached lang=-"))
        self.assertIn('fwd-status=503; ttl=-10; detail="stale-if-error"', h(stale, "Cache-Status"))
        origin.failure = "connect"
        self.assertEqual(get(cache, "/sie").cache_status, "STALE")
        clock.advance(60)
        self.assertEqual(get(cache, "/sie").status, 502)
        origin.failure = 503
        passed = get(cache, "/sie")
        self.assertEqual((passed.status, passed.cache_status), (503, "EXPIRED"))
        self.assertIn("beyond stale-if-error=60", h(passed, "Cache-Status"))

    def test_must_revalidate_beats_stale_if_error_and_a_request_can_ask_for_stale(self):
        cache, origin, clock = setup()
        origin.route("/strict", [("Cache-Control", "max-age=10, must-revalidate, stale-if-error=60")])
        origin.route("/plain", [("Cache-Control", "max-age=10")])
        get(cache, "/strict")
        get(cache, "/plain")
        clock.advance(20)
        origin.failure = "connect"
        self.assertEqual(get(cache, "/strict").status, 504)
        self.assertEqual(get(cache, "/plain").status, 502)
        self.assertEqual(get(cache, "/plain", [("Cache-Control", "stale-if-error=30")]).cache_status, "STALE")

    def test_stale_while_revalidate_answers_at_once_and_refreshes_in_the_background(self):
        cache, origin, clock = setup()
        origin.route("/swr", [("Cache-Control", "max-age=10, stale-while-revalidate=30"), ("ETag", '"v1"')], b"swr")
        get(cache, "/swr")
        clock.advance(15)
        origin.delay = 0.3
        first = get(cache, "/swr")
        second = get(cache, "/swr")
        self.assertEqual((first.cache_status, h(first, "Age"), second.cache_status), ("STALE", "15", "UPDATING"))
        self.assertIn('ttl=-5; detail="stale-while-revalidate"', h(first, "Cache-Status"))
        cache.join()
        self.assertEqual(origin.count("/swr"), 2)
        self.assertEqual(origin.last("/swr").get("if-none-match"), '"v1"')
        refreshed = get(cache, "/swr")
        self.assertEqual((refreshed.cache_status, h(refreshed, "Age")), ("HIT", "0"))

    def test_concurrent_misses_collapse_into_one_origin_request(self):
        cache, origin, _ = setup()
        origin.route("/slow", [("Cache-Control", "max-age=60")], b"slow")
        origin.route("/nostore", [("Cache-Control", "no-store")], b"live")
        origin.delay = 0.3
        for path, want in (("/slow", 1), ("/nostore", 8)):
            results = []
            threads = [threading.Thread(target=lambda: results.append(get(cache, path))) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertEqual(origin.count(path), want, path)
            self.assertEqual({r.status for r in results}, {200})
        words = sorted(r.cache_status for r in results)
        self.assertEqual(words, ["MISS"] * 8)
        cache2, origin2, _ = setup()
        origin2.route("/slow", [("Cache-Control", "max-age=60")], b"slow")
        origin2.delay = 0.3
        collected = []
        threads = [threading.Thread(target=lambda: collected.append(get(cache2, "/slow"))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(r.cache_status for r in collected), ["HIT"] * 7 + ["MISS"])
        self.assertEqual(sum("collapsed" in h(r, "Cache-Status") for r in collected), 7)

    def test_purge_by_url_removes_every_variant_and_by_tag_reads_both_tag_headers(self):
        cache, origin, _ = setup()
        origin.route("/vary", [("Cache-Control", "max-age=60"), ("Vary", "Accept-Language")])
        origin.route("/product", [("Cache-Control", "max-age=60"), ("Surrogate-Key", "product-42 catalogue")])
        origin.route("/home", [("Cache-Control", "max-age=60"), ("Cache-Tag", "home,product-42")])
        get(cache, "/vary", [("Accept-Language", "en")])
        get(cache, "/vary", [("Accept-Language", "de")])
        tagged = get(cache, "/product")
        get(cache, "/home")
        self.assertIsNone(h(tagged, "Surrogate-Key"))
        self.assertEqual(cache.purge_url(BASE + "/vary"), 2)
        self.assertEqual(cache.purge_tag("product-42"), 2)
        self.assertEqual(cache.purge_tag("product-42"), 0)
        self.assertEqual(get(cache, "/product").cache_status, "MISS")

    def test_unsafe_methods_invalidate_the_url_and_a_same_origin_location(self):
        cache, origin, _ = setup()
        origin.route("/cart", [("Cache-Control", "max-age=60")])
        origin.route("/cart/summary", [("Cache-Control", "max-age=60")])
        get(cache, "/cart")
        get(cache, "/cart/summary")
        origin.unsafe_reply = (201, [("Location", "/cart/summary")])
        posted = cache.handle("POST", BASE + "/cart", [], b"item=1")
        self.assertEqual((posted.status, posted.cache_status), (201, "BYPASS"))
        self.assertIn("invalidated 2 stored response(s)", h(posted, "Cache-Status"))
        self.assertEqual(get(cache, "/cart/summary").cache_status, "MISS")
        origin.unsafe_reply = (201, [("Location", "http://elsewhere.test/cart")])
        cache.handle("POST", BASE + "/cart", [], b"item=2")
        self.assertEqual(get(cache, "/cart/summary").cache_status, "HIT")
        origin.unsafe_reply = (500, [])
        cache.handle("POST", BASE + "/cart/summary", [], b"x")
        self.assertEqual(get(cache, "/cart/summary").cache_status, "HIT")

    def test_head_uses_the_get_copy_and_client_conditionals_get_a_304(self):
        cache, origin, _ = setup()
        origin.route("/page", [("Cache-Control", "max-age=60"), ("ETag", '"v1"')], b"hello")
        miss = get(cache, "/page", [("If-None-Match", '"v1"')])
        self.assertEqual((miss.status, miss.body, miss.cache_status), (304, b"", "MISS"))
        self.assertIsNone(origin.last("/page").get("if-none-match"))
        head = get(cache, "/page", method="HEAD")
        self.assertEqual((head.status, head.body, h(head, "Content-Length"), head.cache_status), (200, b"", "12", "HIT"))
        weak = get(cache, "/page", [("If-None-Match", 'W/"v1", "other"')])
        self.assertEqual((weak.status, h(weak, "ETag"), h(weak, "Content-Length")), (304, '"v1"', None))
        self.assertEqual(get(cache, "/page", [("If-None-Match", '"v0"')]).status, 200)

    def test_only_if_cached_answers_504_without_asking_the_origin(self):
        cache, origin, _ = setup()
        origin.route("/page", [("Cache-Control", "max-age=60")])
        refused = get(cache, "/page", [("Cache-Control", "only-if-cached")])
        self.assertEqual((refused.status, origin.count("/page")), (504, 0))
        get(cache, "/page")
        self.assertEqual(get(cache, "/page", [("Cache-Control", "only-if-cached")]).cache_status, "HIT")

    def test_cdn_mode_reads_cdn_cache_control_instead_of_cache_control(self):
        headers = [("Cache-Control", "private, max-age=10"), ("CDN-Cache-Control", "max-age=600")]
        cdn, origin, clock = setup(main.Policy(cdn=True))
        origin.route("/page", headers)
        get(cdn, "/page")
        clock.advance(300)
        self.assertEqual((get(cdn, "/page").cache_status, origin.count("/page")), ("HIT", 1))
        shared, origin2, _ = setup()
        origin2.route("/page", headers)
        self.assertEqual([get(shared, "/page").cache_status for _ in range(2)], ["MISS", "MISS"])

    def test_cache_status_is_rfc_9211_and_appends_to_an_upstream_value(self):
        value = main.cache_status_value("minicache", [("fwd", "stale"), ("fwd-status", 304), ("stored", True), ("collapsed", False), ("ttl", -3), ("detail", 'say "hi"')])
        self.assertEqual(value, 'minicache; fwd=stale; fwd-status=304; stored; ttl=-3; detail="say \\"hi\\""')
        cache, origin, _ = setup()
        origin.route("/page", [("Cache-Control", "max-age=60"), ("Cache-Status", "OriginShield; hit"), ("Via", "1.1 shield")])
        result = get(cache, "/page")
        self.assertTrue(h(result, "Cache-Status").startswith("OriginShield; hit, minicache; fwd=uri-miss"))
        self.assertEqual(h(result, "Via"), "1.1 shield, 1.1 minicache")
        self.assertEqual(h(result, "X-Cache-Status"), "MISS")


class CheckerTests(unittest.TestCase):
    def test_header_blocks_come_from_curl_sI_or_curl_sv_and_the_last_one_wins(self):
        with open(fixture("redirect-verbose.txt"), encoding="utf-8") as handle:
            blocks = main.parse_header_blocks(handle.read())
        self.assertEqual([status for status, _ in blocks], [301, 200])
        self.assertEqual(main.header(blocks[-1][1], "surrogate-key"), "catalogue product-42")
        status, headers = fixture_response("asset-hashed.txt")
        self.assertEqual((status, main.header(headers, "ETag")), (200, '"5f2a-3f9a1c2b"'))
        with self.assertRaises(main.InputError):
            main.parse_header_blocks("HTTP/1.1 200 OK\nthis is not a header\n")
        with self.assertRaises(main.InputError):
            main.parse_header_blocks("\n\n")

    def test_the_cookie_response_is_flagged_for_shared_caches(self):
        status, headers = fixture_response("account-cookie.txt")
        report = main.explain(status, headers, "https://shop.example.test/account", now=T0)
        stores = {name: verdict["store"] for name, verdict in report["audiences"].items()}
        self.assertEqual(stores, {"browser": True, "shared": True, "cdn": False, "nginx": False})
        self.assertEqual({n["code"] for n in report["notes"]}, {"set-cookie", "no-validator"})

    def test_heuristic_freshness_no_cache_and_vary_are_compared_with_nginx(self):
        status, headers = fixture_response("legacy-heuristic.txt")
        report = main.explain(status, headers, "https://shop.example.test/img/logo.png", now=T0)
        self.assertEqual(report["audiences"]["browser"]["lifetime"], 86400)
        self.assertEqual(report["audiences"]["browser"]["after_expiry"], "revalidate with Last-Modified")
        self.assertFalse(report["audiences"]["nginx"]["store"])
        self.assertEqual({n["code"] for n in report["notes"]}, {"heuristic", "pragma", "vary-fragments"})
        status, headers = fixture_response("html-no-cache.txt")
        page = main.explain(status, headers, "https://shop.example.test/", now=T0)
        self.assertTrue(page["audiences"]["cdn"]["revalidate_every_use"])
        self.assertEqual(page["audiences"]["cdn"]["stale_forbidden_by"], "no-cache")
        self.assertIn("no-cache", page["audiences"]["nginx"]["why"])

    def test_age_and_cdn_cache_control_change_what_is_left(self):
        status, headers = fixture_response("redirect-verbose.txt")
        report = main.explain(status, headers, "https://shop.example.test/catalogue/", now=T0)
        audiences = report["audiences"]
        self.assertEqual((audiences["browser"]["remaining"], audiences["cdn"]["remaining"], audiences["nginx"]["remaining"]), (15, 555, 60))
        self.assertEqual((audiences["cdn"]["stale_while_revalidate"], audiences["cdn"]["stale_if_error"]), (60, 86400))
        self.assertEqual(audiences["browser"]["stale_if_error"], 0)
        self.assertEqual({n["code"] for n in report["notes"]}, {"age", "cdn-cache-control", "tags"})

    def test_content_hashes_decide_whether_a_long_lifetime_is_safe(self):
        for url, want in (("/static/app.3f9a1c2b.js", True), ("/assets/index-BqM3xT7f.js", True), ("/css/main-stylesheet.css", False), ("/app.js?v=42", True), ("/logo.png", False)):
            self.assertEqual(main.has_content_hash(url), want, url)
        year = [("Cache-Control", "public, max-age=31536000"), ("ETag", '"a"')]
        self.assertIn("long-unhashed", {n["code"] for n in main.explain(200, year, "/app.js", now=T0)["notes"]})
        short = [("Cache-Control", "max-age=3600"), ("ETag", '"a"')]
        self.assertIn("hashed-short", {n["code"] for n in main.explain(200, short, "/app.3f9a1c2b.js", now=T0)["notes"]})
        status, headers = fixture_response("asset-hashed.txt")
        self.assertEqual(main.explain(status, headers, "/static/app.3f9a1c2b.js", now=T0)["notes"], [])

    def test_the_nginx_model_follows_its_source(self):
        verdict = main.nginx_verdict
        self.assertFalse(verdict(200, [("X-Accel-Expires", "0"), ("Cache-Control", "max-age=60")], T0)["store"])
        accel = verdict(200, [("X-Accel-Expires", "30"), ("Cache-Control", "private")], T0)
        self.assertEqual((accel["store"], accel["lifetime"]), (True, 30))
        self.assertFalse(verdict(200, [("Cache-Control", "max-age=0")], T0)["store"])
        self.assertEqual(verdict(200, [("Cache-Control", "max-age=10, s-maxage=90")], T0)["lifetime"], 90)
        windows = verdict(200, [("Cache-Control", "max-age=10, stale-while-revalidate=20")], T0)
        self.assertEqual((windows["stale_while_revalidate"], windows["stale_if_error"]), (20, 20))
        both = verdict(200, [("Cache-Control", "max-age=10, stale-while-revalidate=20, stale-if-error=5")], T0)
        self.assertEqual(both["stale_if_error"], 5)
        expires = verdict(200, [("Expires", main.http_date(T0 + 100))], T0)
        self.assertEqual((expires["store"], expires["lifetime"]), (True, 100))
        self.assertFalse(verdict(200, [("Expires", main.http_date(T0 - 1))], T0)["store"])
        self.assertIn("proxy_cache_valid", verdict(200, [("Last-Modified", main.http_date(T0 - 86400))], T0)["why"])


class ServerTests(unittest.TestCase):
    def setUp(self):
        routes = dict(main.DEFAULT_ROUTES)
        routes["slow"] = {"cache_control": "max-age=60", "delay": 0.3}
        self.origin = main.OriginServer(("127.0.0.1", 0), routes)
        self.clock = Clock(time.time())
        origin_url = f"http://127.0.0.1:{self.origin.server_address[1]}"
        self.cache = main.Cache(main.origin_fetcher(origin_url, timeout=5), main.Policy(), clock=self.clock)
        self.proxy = main.CacheServer(("127.0.0.1", 0), self.cache)
        for server in (self.origin, self.proxy):
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def request(self, method, path, headers=None, port=None):
        connection = http.client.HTTPConnection("127.0.0.1", port or self.proxy.server_address[1], timeout=10)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            connection.close()

    def origin_stats(self):
        return json.loads(self.request("GET", "/__origin/stats", port=self.origin.server_address[1])[2])

    def test_the_proxy_caches_revalidates_purges_and_invalidates_over_real_sockets(self):
        status, headers, body = self.request("GET", "/t/fresh")
        self.assertEqual((status, headers["x-cache-status"], body), (200, "MISS", b"fresh v1 lang=-\n"))
        status, headers, _ = self.request("GET", "/t/fresh")
        self.assertEqual((headers["x-cache-status"], "age" in headers), ("HIT", True))
        self.clock.advance(5)
        self.assertEqual(self.request("GET", "/t/fresh")[1]["x-cache-status"], "REVALIDATED")
        seen = self.origin_stats()["paths"]["/t/fresh"]
        self.assertEqual([r["status"] for r in seen["requests"]], [200, 304])
        self.assertEqual(seen["requests"][1]["if_none_match"], '"fresh-v1"')
        status, _, body = self.request("PURGE", "/t/fresh")
        self.assertEqual((status, json.loads(body)["purged"]), (200, 1))
        self.assertEqual(self.request("PURGE", "/t/fresh")[0], 404)
        self.request("GET", "/t/fresh")
        status, headers, _ = self.request("POST", "/t/fresh", {"Content-Length": "0"})
        self.assertEqual((status, headers["x-cache-status"]), (200, "BYPASS"))
        status, headers, body = self.request("GET", "/t/fresh")
        self.assertEqual((headers["x-cache-status"], body), ("MISS", b"fresh v2 lang=-\n"))
        entries = json.loads(self.request("GET", "/.minicache/entries")[2])
        self.assertEqual([e["etag"] for e in entries["entries"]], ['"fresh-v2"'])

    def test_stale_if_error_collapse_and_tags_against_the_real_origin(self):
        self.request("GET", "/t/sie")
        self.request("GET", "/t/tagged")
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.request("GET", "/t/slow"))) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(r[1]["x-cache-status"] for r in results), ["HIT"] * 5 + ["MISS"])
        status, _, body = self.request("PURGE", "/.minicache/tag/product-42")
        self.assertEqual((status, json.loads(body)["purged"]), (200, 1))
        self.request("POST", "/__origin/down", port=self.origin.server_address[1])
        self.clock.advance(5)
        status, headers, body = self.request("GET", "/t/sie")
        self.assertEqual((status, headers["x-cache-status"], body), (200, "STALE", b"sie v1 lang=-\n"))
        stats = self.origin_stats()
        self.assertEqual((stats["paths"]["/t/slow"]["count"], stats["paths"]["/t/sie"]["requests"][-1]["status"]), (1, 503))


class CliTests(unittest.TestCase):
    def run_main(self, argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_check_prints_text_and_json(self):
        code, out, _ = self.run_main(["check", fixture("account-cookie.txt"), "--url", "https://shop.example.test/account"])
        self.assertEqual(code, 0)
        self.assertIn("[set-cookie]", out)
        self.assertRegex(out, r"nginx\s+no\s+-\s+Set-Cookie")
        code, out, _ = self.run_main(["check", fixture("redirect-verbose.txt"), "--json"])
        self.assertEqual((code, json.loads(out)["audiences"]["cdn"]["remaining"] > 0), (0, True))
        code, out, _ = self.run_main(["origin", "--list", "--json"])
        self.assertEqual((code, json.loads(out)["fresh"]["cache_control"]), (0, "max-age=2"))

    def test_bad_input_exits_two_with_the_reason(self):
        missing = os.path.join(tempfile.gettempdir(), "minicache-missing-headers.txt")
        code, _, err = self.run_main(["check", missing])
        self.assertEqual(code, 2)
        self.assertIn("No such file", err)
        code, _, err = self.run_main(["check", fixture("asset-hashed.txt"), "-H", "no-colon"])
        self.assertEqual((code, "expected 'Name: value'" in err), (2, True))
        code, _, err = self.run_main(["serve", "--origin", "ftp://example.test"])
        self.assertEqual((code, "expected http://HOST:PORT" in err), (2, True))
        code, _, err = self.run_main(["serve", "--origin", "http://127.0.0.1:9", "--private", "--cdn"])
        self.assertEqual((code, "exclude each other" in err), (2, True))
        folder = tempfile.mkdtemp(prefix="minicache-")
        self.addCleanup(shutil.rmtree, folder)
        bad = os.path.join(folder, "routes.json")
        with open(bad, "w", encoding="utf-8") as handle:
            json.dump({"fresh": {"max_age": 5}}, handle)
        code, _, err = self.run_main(["origin", "--routes", bad, "--list"])
        self.assertEqual((code, "unknown field 'max_age'" in err), (2, True))

    def test_an_unreachable_url_exits_one(self):
        code, _, err = self.run_main(["check", "http://127.0.0.1:9/nothing"])
        self.assertEqual(code, 1)
        self.assertIn("127.0.0.1:9", err)


if __name__ == "__main__":
    unittest.main()
