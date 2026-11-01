import dataclasses
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

HELLO = b"minihttp and nginx serve these exact bytes from the same file.\n"
MTIME = 1767323045


def head_of(text, **overrides):
    config = dataclasses.replace(main.Config(), **overrides)
    return main.parse_head(text.replace("\n", "\r\n").encode("latin-1"), config)


def status_of(text, **overrides):
    with self_raises() as caught:
        head_of(text, **overrides)
    return caught.status


class self_raises:
    def __enter__(self):
        self.status = None
        return self

    def __exit__(self, kind, exc, tb):
        if kind is None:
            raise AssertionError("expected an HTTPError")
        if not issubclass(kind, main.HTTPError):
            return False
        self.status = exc.status
        self.reason = exc.reason
        return True


def wire_of(data, piece=65536):
    return main.Wire(main.ByteSocket(data, piece))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def run_cli(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class ParseTests(unittest.TestCase):
    def test_request_line_fields_and_defaults(self):
        head = head_of("GET /docs/a%20b.txt?x=1 HTTP/1.1\nHost: shop.test:8080\nAccept:  */*  \nX-Two: a\nX-Two: b\n\n")
        self.assertEqual((head.method, head.target, head.version), ("GET", "/docs/a%20b.txt?x=1", (1, 1)))
        self.assertEqual((head.path, head.query, head.host), ("/docs/a%20b.txt", "x=1", "shop.test:8080"))
        self.assertEqual(head.header("accept"), "*/*")
        self.assertEqual(head.values("x-two"), ["a", "b"])
        self.assertEqual((head.framing, head.keep_alive, head.expect_continue), ("none", True, False))
        absolute = head_of("GET http://other.test/x?y HTTP/1.1\nHost: ignored.test\n\n")
        self.assertEqual((absolute.path, absolute.query, absolute.host), ("/x", "y", "other.test"))

    def test_bare_lf_and_leading_blank_lines_are_tolerated_a_bare_cr_is_not(self):
        wire = wire_of(b"\r\n\r\nGET / HTTP/1.1\nHost: a\n\n")
        raw = wire.read_head(lambda: 1.0, 8192, 32768)
        self.assertEqual(main.parse_head(raw, main.Config()).path, "/")
        with self_raises() as caught:
            main.parse_head(b"GET / HTTP/1.1\r\nHost: a\rX-Evil: 1\r\n\r\n", main.Config())
        self.assertEqual(caught.status, 400)
        self.assertIn("bare CR", caught.reason)

    def test_request_line_errors_map_to_400_and_505(self):
        self.assertEqual(status_of("GET  / HTTP/1.1\nHost: a\n\n"), 400)
        self.assertEqual(status_of("GET / http/1.1\nHost: a\n\n"), 400)
        self.assertEqual(status_of("GET /\nHost: a\n\n"), 400)
        self.assertEqual(status_of("GET / HTTP/2.0\nHost: a\n\n"), 505)
        self.assertEqual(status_of("GET ../x HTTP/1.1\nHost: a\n\n"), 400)
        self.assertEqual(status_of("GET * HTTP/1.1\nHost: a\n\n"), 400)
        self.assertEqual(status_of("CONNECT a:443 HTTP/1.1\nHost: a\n\n"), 405)
        self.assertEqual(head_of("OPTIONS * HTTP/1.1\nHost: a\n\n").path, "*")
        self.assertEqual(head_of("GET / HTTP/1.9\nHost: a\n\n").version, (1, 9))

    def test_field_syntax_rules(self):
        with self_raises() as caught:
            head_of("GET / HTTP/1.1\nHost : a\n\n")
        self.assertEqual(caught.status, 400)
        self.assertIn("before the colon", caught.reason)
        self.assertEqual(status_of("GET / HTTP/1.1\nHost: a\nX-Long: one\n two\n\n"), 400)
        self.assertEqual(status_of("GET / HTTP/1.1\nHost: a\nno colon here\n\n"), 400)
        self.assertEqual(status_of("GET / HTTP/1.1\nHost: a\nX-Bad: a\x01b\n\n"), 400)
        self.assertEqual(status_of("GET / HTTP/1.1\n\n"), 400)
        self.assertEqual(status_of("GET / HTTP/1.1\nHost: a\nHost: b\n\n"), 400)
        self.assertEqual(status_of("GET / HTTP/1.1\nHost: a b\n\n"), 400)
        self.assertEqual(head_of("GET / HTTP/1.0\n\n").host, "")
        many = "".join(f"X-{i}: v\n" for i in range(5))
        self.assertEqual(status_of(f"GET / HTTP/1.1\nHost: a\n{many}\n", max_headers=5), 431)

    def test_both_length_headers_are_refused_so_no_second_request_is_smuggled(self):
        with self_raises() as caught:
            head_of("POST / HTTP/1.1\nHost: a\nContent-Length: 4\nTransfer-Encoding: chunked\n\n")
        self.assertEqual(caught.status, 400)
        self.assertIn("both Content-Length and Transfer-Encoding", caught.reason)
        stream = b"POST /a HTTP/1.1\r\nHost: a\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\nGET /admin HTTP/1.1\r\nHost: a\r\n\r\n"
        found = main.parse_stream(stream, main.Config())
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["status"], found[0]["close"]), (400, True))

    def test_transfer_encoding_and_content_length_rules(self):
        self.assertEqual(head_of("POST / HTTP/1.1\nHost: a\nTransfer-Encoding: Chunked\n\n").framing, "chunked")
        self.assertEqual(status_of("POST / HTTP/1.0\nTransfer-Encoding: chunked\n\n"), 400)
        self.assertEqual(status_of("POST / HTTP/1.1\nHost: a\nTransfer-Encoding: gzip, chunked\n\n"), 501)
        self.assertEqual(status_of("POST / HTTP/1.1\nHost: a\nTransfer-Encoding: chunked, gzip\n\n"), 400)
        self.assertEqual(status_of("POST / HTTP/1.1\nHost: a\nTransfer-Encoding: chunked\nTransfer-Encoding: chunked\n\n"), 400)
        self.assertEqual(status_of("POST / HTTP/1.1\nHost: a\nContent-Length: 5\nContent-Length: 5\n\n"), 400)
        self.assertEqual(status_of("POST / HTTP/1.1\nHost: a\nContent-Length: 5, 5\n\n"), 400)
        self.assertEqual(status_of("POST / HTTP/1.1\nHost: a\nContent-Length: +5\n\n"), 400)
        self.assertEqual(status_of("POST / HTTP/1.1\nHost: a\nContent-Length: 101\n\n", max_body=100), 413)
        head = head_of("POST / HTTP/1.1\nHost: a\nContent-Length: 12\n\n")
        self.assertEqual((head.framing, head.length), ("length", 12))
        self.assertEqual(head_of("POST / HTTP/1.1\nHost: a\nContent-Length: 0\n\n").framing, "none")

    def test_persistence_rules(self):
        self.assertTrue(main.persistent((1, 1), []))
        self.assertFalse(main.persistent((1, 1), ["close"]))
        self.assertFalse(main.persistent((1, 0), []))
        self.assertTrue(main.persistent((1, 0), ["keep-alive"]))
        self.assertFalse(head_of("GET / HTTP/1.1\nHost: a\nConnection: Upgrade, CLOSE\n\n").keep_alive)
        self.assertTrue(head_of("POST / HTTP/1.1\nHost: a\nContent-Length: 3\nExpect: 100-Continue\n\n").expect_continue)
        self.assertFalse(head_of("GET / HTTP/1.1\nHost: a\nExpect: 100-continue\n\n").expect_continue)

    def test_head_size_limits_give_414_and_431(self):
        with self_raises() as caught:
            wire_of(b"GET /" + b"a" * 100 + b" HTTP/1.1\r\n\r\n").read_head(lambda: 1.0, 50, 1000)
        self.assertEqual(caught.status, 414)
        with self_raises() as caught:
            wire_of(b"GET / HTTP/1.1\r\n" + b"X: " + b"v" * 2000 + b"\r\n\r\n", piece=100).read_head(lambda: 1.0, 8192, 1000)
        self.assertEqual(caught.status, 431)
        with self.assertRaises(main.Closed):
            wire_of(b"GET / HTTP/1.1\r\nHost: a\r\n").read_head(lambda: 1.0, 8192, 1000)


class BodyTests(unittest.TestCase):
    def test_chunked_decoding_with_extensions_and_trailers(self):
        wire = wire_of(b"5;name=value\r\nhello\r\n7 \r\n, world\r\n0\r\nX-Checksum: abc\r\n\r\nNEXT", piece=3)
        self.assertEqual(main.read_chunked(wire, 1.0, 100, 1000), b"hello, world")
        self.assertEqual(bytes(wire.buffer) + wire.sock.data[wire.sock.offset:], b"NEXT")

    def test_chunked_errors(self):
        cases = [
            (b"zz\r\nhello\r\n0\r\n\r\n", 400),
            (b"5\nhello\r\n0\r\n\r\n", 400),
            (b"5\r\nhelloXX0\r\n\r\n", 400),
            (b"8\r\n12345678\r\n0\r\n\r\n", 413),
            (b"-1\r\n", 400),
        ]
        for data, status in cases:
            with self.subTest(data=data):
                with self_raises() as caught:
                    main.read_chunked(wire_of(data), 1.0, 5, 1000)
                self.assertEqual(caught.status, status)

    def test_expect_100_continue_is_sent_only_while_the_body_has_not_started(self):
        config = main.Config()
        head = head_of("POST / HTTP/1.1\nHost: a\nContent-Length: 5\nExpect: 100-continue\n\n")
        quiet = main.ByteSocket(b"hello")
        self.assertEqual(main.read_body(main.Wire(quiet), head, config), b"hello")
        self.assertEqual(bytes(quiet.sent), b"HTTP/1.1 100 Continue\r\n\r\n")
        early = main.Wire(main.ByteSocket(b""))
        early.buffer += b"hello"
        self.assertEqual(main.read_body(early, head, config), b"hello")
        self.assertEqual(bytes(early.sock.sent), b"")

    def test_response_framing_interim_replies_and_close_delimited_bodies(self):
        data = b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n"
        reply = main.read_response(wire_of(data), "POST", 1.0)
        self.assertEqual((reply.status, reply.interim, reply.body), (200, [100], b"abc"))
        self.assertEqual(main.read_response(wire_of(b"HTTP/1.0 200 OK\r\n\r\nuntil close"), "GET", 1.0).body, b"until close")
        self.assertEqual(main.read_response(wire_of(b"HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\n"), "HEAD", 1.0).body, b"")
        self.assertEqual(main.read_response(wire_of(b"HTTP/1.1 304 Not Modified\r\nContent-Length: 99\r\n\r\n"), "GET", 1.0).body, b"")
        with self_raises() as caught:
            main.read_response(wire_of(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\nab"), "GET", 1.0)
        self.assertEqual(caught.status, 502)
        with self.assertRaises(main.Closed):
            main.read_response(wire_of(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nshort"), "GET", 1.0)


class StaticTests(unittest.TestCase):
    def test_normalize_path(self):
        self.assertEqual(main.normalize_path("/a//b/./c"), "/a/b/c")
        self.assertEqual(main.normalize_path("/docs/../hello.txt"), "/hello.txt")
        self.assertEqual(main.normalize_path("/docs/"), "/docs/")
        self.assertEqual(main.normalize_path("/docs/x/.."), "/docs/")
        self.assertEqual(main.normalize_path("/%68ello%20world.txt"), "/hello world.txt")
        self.assertEqual(main.normalize_path("/"), "/")
        for bad in ("/../etc/passwd", "/a/%2e%2e/%2e%2e/etc", "/a%00b", "/%ff"):
            with self.subTest(path=bad):
                with self_raises() as caught:
                    main.normalize_path(bad)
                self.assertEqual(caught.status, 400)

    def test_etag_uses_nginx_format_and_rfc_comparisons(self):
        self.assertEqual(main.make_etag(1767323045.9, 63), '"695735a5-3f"')
        self.assertTrue(main.etag_matches('"x", "695735a5-3f"', '"695735a5-3f"', weak=True))
        self.assertTrue(main.etag_matches('W/"695735a5-3f"', '"695735a5-3f"', weak=True))
        self.assertFalse(main.etag_matches('W/"695735a5-3f"', '"695735a5-3f"', weak=False))
        self.assertTrue(main.etag_matches("*", '"anything"', weak=False))
        self.assertFalse(main.etag_matches('"a,b"', '"a"', weak=True))
        self.assertTrue(main.etag_matches('"a,b"', '"a,b"', weak=True))

    def test_preconditions_follow_rfc_9110_order(self):
        etag, mtime = '"e-1"', MTIME
        date = main.http_date(MTIME)
        before, after = main.http_date(MTIME - 60), main.http_date(MTIME + 60)

        def verdict(method="GET", **fields):
            lines = "".join(f"{k.replace('_', '-')}: {v}\n" for k, v in fields.items())
            return main.evaluate_preconditions(head_of(f"{method} / HTTP/1.1\nHost: a\n{lines}\n"), etag, mtime)

        self.assertEqual(verdict(If_None_Match=etag), 304)
        self.assertEqual(verdict("POST", If_None_Match=etag), 412)
        self.assertIsNone(verdict(If_None_Match='"other"'))
        self.assertEqual(verdict(If_Modified_Since=date), 304)
        self.assertEqual(verdict(If_Modified_Since=after), 304)
        self.assertIsNone(verdict(If_Modified_Since=before))
        self.assertIsNone(verdict(If_None_Match='"other"', If_Modified_Since=after))
        self.assertIsNone(verdict(If_Modified_Since="not a date"))
        self.assertEqual(verdict(If_Match='"other"'), 412)
        self.assertEqual(verdict(If_Unmodified_Since=before), 412)
        self.assertIsNone(verdict(If_Match=etag, If_Unmodified_Since=before))

    def test_parse_range_cases(self):
        self.assertEqual(main.parse_range("bytes=0-4", 63), [(0, 4)])
        self.assertEqual(main.parse_range("bytes=60-100", 63), [(60, 62)])
        self.assertEqual(main.parse_range("bytes=10-", 63), [(10, 62)])
        self.assertEqual(main.parse_range("bytes=-5", 63), [(58, 62)])
        self.assertEqual(main.parse_range("bytes=-500", 63), [(0, 62)])
        self.assertEqual(main.parse_range("bytes=63-", 63), [])
        self.assertEqual(main.parse_range("bytes=-0", 63), [])
        self.assertEqual(main.parse_range("bytes=0-1, 4-5", 63), [(0, 1), (4, 5)])
        self.assertIsNone(main.parse_range("bytes=5-2", 63))
        self.assertIsNone(main.parse_range("bytes=abc", 63))
        self.assertIsNone(main.parse_range("items=0-1", 63))

    def test_choose_range_honours_if_range_and_ignores_multiple_ranges(self):
        etag, date = '"e-1"', main.http_date(MTIME)

        def chosen(*fields, method="GET"):
            lines = "".join(f"{line}\n" for line in fields)
            return main.choose_range(head_of(f"{method} / HTTP/1.1\nHost: a\n{lines}\n"), 63, etag, date)

        self.assertEqual(chosen("Range: bytes=0-4"), (0, 4))
        self.assertEqual(chosen("Range: bytes=0-4", f"If-Range: {etag}"), (0, 4))
        self.assertEqual(chosen("Range: bytes=0-4", f"If-Range: {date}"), (0, 4))
        self.assertIsNone(chosen("Range: bytes=0-4", 'If-Range: "stale"'))
        self.assertIsNone(chosen("Range: bytes=0-4", f"If-Range: W/{etag}"))
        self.assertIsNone(chosen("Range: bytes=0-1,4-5"))
        self.assertIsNone(chosen("Range: bytes=0-4", method="HEAD"))
        self.assertEqual(chosen("Range: bytes=99-"), main.UNSATISFIABLE)


class ProxyHeaderTests(unittest.TestCase):
    def test_forward_headers_drop_hop_by_hop_and_append_forwarding_fields(self):
        head = head_of("POST /api/x HTTP/1.1\nHost: shop.test\nConnection: keep-alive, X-Hop\nX-Hop: secret\nKeep-Alive: timeout=5\nTE: trailers\nUpgrade: h2c\n"
                       "Transfer-Encoding: chunked\nExpect: 100-continue\nX-Forwarded-For: 203.0.113.7\nVia: 1.0 edge\nX-Kept: yes\n\n")
        fields = main.forward_headers(head, "10.0.0.9", b"hello", main.Upstream("127.0.0.1", 9000), "minihttp")
        names = [key.lower() for key, _ in fields]
        for gone in ("x-hop", "keep-alive", "te", "upgrade", "transfer-encoding", "expect"):
            self.assertNotIn(gone, names)
        found = dict((key.lower(), value) for key, value in fields)
        self.assertEqual(found["host"], "shop.test")
        self.assertEqual(found["x-forwarded-for"], "203.0.113.7, 10.0.0.9")
        self.assertEqual(found["via"], "1.0 edge, 1.1 minihttp")
        self.assertEqual((found["content-length"], found["connection"], found["x-kept"]), ("5", "close", "yes"))
        bare = main.forward_headers(head_of("GET / HTTP/1.0\n\n"), "10.0.0.9", b"", main.Upstream("127.0.0.1", 9000), "minihttp")
        self.assertEqual(bare[0], ("Host", "127.0.0.1:9000"))
        self.assertNotIn("content-length", [key.lower() for key, _ in bare])

    def test_backward_headers_drop_upstream_date_server_and_framing(self):
        upstream = [("Date", "x"), ("Server", "gunicorn"), ("Transfer-Encoding", "chunked"), ("Connection", "X-Internal"), ("X-Internal", "1"), ("Content-Length", "9"), ("ETag", '"a"'), ("Via", "1.1 app")]
        kept = main.backward_headers(upstream, "minihttp", keep_length=False)
        self.assertEqual(kept, [("ETag", '"a"'), ("Via", "1.1 app, 1.1 minihttp")])
        self.assertIn(("Content-Length", "9"), main.backward_headers(upstream, "minihttp", keep_length=True))


class LogAndConfigTests(unittest.TestCase):
    def test_access_log_is_nginx_combined_format_and_parses_back(self):
        line = main.log_line("127.0.0.1", 'GET /a"b HTTP/1.1', 206, 5, "", "curl/8.5.0", when=MTIME)
        self.assertRegex(line, r'^127\.0\.0\.1 - - \[02/Jan/2026:\d\d:\d\d:05 [+-]\d{4}\] "GET /a\\x22b HTTP/1\.1" 206 5 "-" "curl/8\.5\.0"$')
        nginx = '172.17.0.1 - - [02/Nov/2026:10:00:00 +0000] "GET /hello.txt HTTP/1.1" 408 0 "-" "-"'
        self.assertEqual(main.parse_log_line(nginx)["status"], 408)
        stats = main.log_stats([line, nginx, "/docker-entrypoint.sh: Configuration complete", ""])
        self.assertEqual((stats["parsed"], stats["skipped"], stats["status"]), (2, 1, {"206": 1, "408": 1}))

    def test_nginx_conf_maps_every_setting(self):
        config = main.Config(header_timeout=2, body_timeout=1.5, keepalive_timeout=3, keepalive_requests=3, max_body=65536, max_headers=50, upstream_connect_timeout=2, upstream_timeout=2, proxies=[("/api/", "http://127.0.0.1:9000")])
        text = main.nginx_conf(config, "/srv/www", "127.0.0.1:8081")
        for directive in ("client_header_timeout 2s;", "client_body_timeout 1500ms;", "keepalive_timeout 3s;", "keepalive_requests 3;", "client_max_body_size 65536;",
                          "max_headers 50;", "large_client_header_buffers 4 8192;", "listen 127.0.0.1:8081;", "root /srv/www;", "location /api/ {",
                          "proxy_pass http://127.0.0.1:9000;", "proxy_read_timeout 2s;", "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;", 'default "$http_via, 1.1 nginx";'):
            self.assertIn(directive, text)
        self.assertEqual(text.count("{"), text.count("}"))
        report = main.config_report(config)
        self.assertEqual(report["nginx_equivalent"]["client_header_timeout"], "2s")
        self.assertEqual(report["nginx_locations"]["/api/"]["proxy_pass"], "http://127.0.0.1:9000")

    def test_cli_config_parse_and_input_errors(self):
        code, out, _ = run_cli(["config", "--json", "--header-timeout", "2", "--proxy", "/api/=http://127.0.0.1:9000"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["minihttp"]["proxies"], [{"prefix": "/api/", "upstream": "http://127.0.0.1:9000"}])
        stream = tempfile.NamedTemporaryFile(delete=False, suffix=".http")
        self.addCleanup(os.unlink, stream.name)
        stream.write(b"GET /a HTTP/1.1\r\nHost: a\r\n\r\nGET /b HTTP/1.1\r\nHost: a\r\nConnection: close\r\n\r\nGET /never HTTP/1.1\r\n\r\n")
        stream.close()
        code, out, _ = run_cli(["parse", stream.name, "--json"])
        found = json.loads(out)
        self.assertEqual([entry.get("target") for entry in found], ["/a", "/b", None])
        self.assertIn("never read", found[2]["reason"])
        for argv, needle in ((["config", "--proxy", "api=http://x"], "expected /PREFIX/="), (["config", "--proxy", "/api/=http://x:1/v1"], "drop the path"),
                             (["config", "--header-timeout", "0"], "greater than 0"), (["parse", "/nonexistent/minihttp.raw"], "No such file"),
                             (["probe", "--target", "nowhere"], "HOST:PORT")):
            with self.subTest(argv=argv):
                code, _, err = run_cli(argv)
                self.assertEqual(code, 2)
                self.assertIn(needle, err)


class LiveServer(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="minihttp-")
        self.addCleanup(shutil.rmtree, self.root)
        os.makedirs(os.path.join(self.root, "docs"))
        hello = os.path.join(self.root, "hello.txt")
        with open(hello, "wb") as handle:
            handle.write(HELLO)
        with open(os.path.join(self.root, "docs", "index.html"), "wb") as handle:
            handle.write(b"<p>docs</p>\n")
        os.utime(hello, (MTIME, MTIME))
        self.upstream = main.Server(main.Config(port=0, name="upstream"), app=main.echo_app).start()
        self.addCleanup(self.upstream.stop)
        self.config = main.Config(root=self.root, port=0, header_timeout=0.6, body_timeout=0.6, keepalive_timeout=0.8, keepalive_requests=3, max_body=4096,
                                  upstream_connect_timeout=0.5, upstream_timeout=0.5,
                                  proxies=[("/api/", f"http://127.0.0.1:{self.upstream.address[1]}"), ("/dead/", f"http://127.0.0.1:{free_port()}")])
        self.log = []
        self.server = main.Server(self.config, log=self.log.append).start()
        self.addCleanup(self.server.stop)
        self.ctx = main.ProbeContext(self.server.address, self.config, timeout=3.0)

    def ask(self, method, target, headers=(), body=b"", **kwargs):
        return main.one(self.ctx, method, target, headers, body, **kwargs)


class LiveTests(LiveServer):
    def test_keepalive_reuse_request_limit_and_idle_close(self):
        with main.Client(self.ctx) as client:
            replies = [client.ask(main.raw_request("GET", "/hello.txt")) for _ in range(3)]
            closed = client.closed_within(1.0)
        self.assertEqual([r.status for r in replies], [200, 200, 200])
        self.assertEqual([r.header("connection") for r in replies], ["", "", "close"])
        self.assertIsNotNone(closed)
        with main.Client(self.ctx) as client:
            self.assertEqual(client.ask(main.raw_request("GET", "/hello.txt")).status, 200)
            idle = client.closed_within(3.0)
        self.assertIsNotNone(idle)
        self.assertGreater(idle, 0.6)
        self.assertLess(idle, 1.6)

    def test_slow_header_sender_is_cut_off_at_the_deadline_not_per_read(self):
        started = time.monotonic()
        result = main.probe_slow_header(self.ctx)
        self.assertEqual(result["status"], [408])
        self.assertGreaterEqual(result["drips"], 3)
        self.assertGreater(result["closed_after"], 0.5)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(any('"GET /hello.txt HTTP/1.1" 408 ' in line for line in self.log))

    def test_static_ranges_validators_and_head_over_a_socket(self):
        full = self.ask("GET", "/hello.txt")
        self.assertEqual((full.status, full.body, full.header("etag")), (200, HELLO, main.make_etag(MTIME, len(HELLO))))
        self.assertEqual(full.header("last-modified"), main.http_date(MTIME))
        part = self.ask("GET", "/hello.txt", [("Range", "bytes=0-7")])
        self.assertEqual((part.status, part.body, part.header("content-range")), (206, b"minihttp", f"bytes 0-7/{len(HELLO)}"))
        self.assertEqual(self.ask("GET", "/hello.txt", [("Range", "bytes=-2")]).body, b".\n")
        miss = self.ask("GET", "/hello.txt", [("Range", "bytes=999-")])
        self.assertEqual((miss.status, miss.header("content-range")), (416, f"bytes */{len(HELLO)}"))
        cached = self.ask("GET", "/hello.txt", [("If-None-Match", full.header("etag"))])
        self.assertEqual((cached.status, cached.body, cached.header("content-length"), cached.header("etag")), (304, b"", "", full.header("etag")))
        with main.Client(self.ctx) as client:
            head = client.ask(main.raw_request("HEAD", "/hello.txt"), "HEAD")
            after = client.ask(main.raw_request("GET", "/hello.txt"))
        self.assertEqual((head.status, head.header("content-length"), head.body, after.body), (200, str(len(HELLO)), b"", HELLO))
        moved = self.ask("GET", "/docs", host="shop.test")
        self.assertEqual((moved.status, moved.header("location")), (301, "http://shop.test/docs/"))
        self.assertEqual(self.ask("GET", "/docs/").body, b"<p>docs</p>\n")
        self.assertEqual(self.ask("POST", "/hello.txt", [("Content-Length", "0")]).header("allow"), "GET, HEAD")

    def test_proxy_upload_forwarding_and_gateway_errors(self):
        body = b"4\r\nwiki\r\n5\r\npedia\r\n0\r\n\r\n"
        reply = self.ask("POST", "/api/echo", [("Transfer-Encoding", "chunked"), ("X-Forwarded-For", "203.0.113.7")], body)
        seen = main.upstream_view(reply)
        self.assertEqual((seen["framing"], seen["body_length"], seen["fields"]["content-length"]), ("length", 9, "9"))
        self.assertEqual(seen["fields"]["x-forwarded-for"], "203.0.113.7, 127.0.0.1")
        self.assertNotIn("transfer-encoding", seen["fields"])
        self.assertEqual(reply.header("via"), "1.1 minihttp")
        chunked = self.ask("GET", "/api/chunked")
        self.assertEqual((chunked.status, chunked.body, chunked.header("transfer-encoding")), (200, b"alpha beta gamma\n", ""))
        started = time.monotonic()
        slow = self.ask("GET", "/api/slow?ms=1500")
        self.assertEqual(slow.status, 504)
        self.assertLess(time.monotonic() - started, 1.2)
        dead = self.ask("GET", "/dead/x")
        self.assertEqual(dead.status, 502)
        self.assertIn("connecting to", dead.body.decode())

    def test_a_smuggling_attempt_gets_one_400_and_a_closed_connection(self):
        result = main.probe_both_lengths(self.ctx)
        self.assertEqual(result, {"status": [400], "closed": True, "responses": 1})
        self.assertFalse(any("/hello.txt" in line and '" 200 ' in line for line in self.log))

    def test_413_and_501_reach_the_client_before_the_connection_closes(self):
        with main.Client(self.ctx) as client:
            client.send(main.raw_request("POST", "/api/echo", [("Content-Length", "100000")]) + b"x" * 30000)
            reply = client.reply()
            self.assertEqual((reply.status, reply.header("connection")), (413, "close"))
            self.assertIsNotNone(client.closed_within(3.0))
        self.assertEqual(self.ask("POST", "/api/echo", [("Transfer-Encoding", "gzip, chunked")], b"0\r\n\r\n").status, 501)

    def test_expect_continue_through_the_proxy(self):
        result = main.probe_expect(self.ctx)
        self.assertEqual((result["status"], result["interim"], result["upstream_body_length"], result["upstream_expect"]), ([200], [100], 11, ""))


class ProbeTests(LiveServer):
    def test_the_whole_probe_matrix_meets_minihttps_expectations(self):
        results = main.run_probe(self.ctx)
        self.assertEqual(len(results), len(main.PROBE_EXPECT))
        differing = [(r["case"], r["status"], r.get("error")) for r in results if r["verdict"] != "ok"]
        self.assertEqual(differing, [])
        by_case = {r["case"]: r for r in results}
        self.assertEqual(by_case["forwarding"]["x_hop"], "")
        self.assertEqual(by_case["forwarding"]["via"], "1.0 edge, 1.1 minihttp")
        self.assertEqual(by_case["chunked-upload"]["upstream_sha256"], by_case["chunked-upload"]["sent_sha256"])
        self.assertEqual(by_case["range-416"]["content_range"], f"bytes */{len(HELLO)}")
        code, out, _ = run_cli(["probe", "--target", "%s:%d" % self.server.address, "--only", "range", "--only", "etag-304", "--strict"])
        self.assertEqual(code, 0)
        self.assertIn("2 cases, 0 differ", out)


if __name__ == "__main__":
    unittest.main()
