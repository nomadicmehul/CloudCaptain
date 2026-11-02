import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import re
import secrets
import socket
import ssl
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
PKI = os.path.join(FIXTURES, "pki")
NOW = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
SMALL_PRIMES = [p for p in range(3, 2000, 2) if all(p % d for d in range(3, int(p ** 0.5) + 1, 2))]


def pem(name):
    return os.path.join(PKI, name)


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


def cert(name):
    return main.load_certificates(read(pem(name)), name)[0]


def capture(name):
    return main.load_bytes(os.path.join(FIXTURES, name))


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def probable_prime(candidate):
    if any(candidate % p == 0 for p in SMALL_PRIMES):
        return False
    d, r = candidate - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(16):
        x = pow(secrets.randbelow(candidate - 3) + 2, d, candidate)
        if x in (1, candidate - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, candidate)
            if x == candidate - 1:
                break
        else:
            return False
    return True


def rsa_key(bits=1024):
    while True:
        primes = []
        while len(primes) < 2:
            candidate = secrets.randbits(bits // 2) | (3 << (bits // 2 - 2)) | 1
            if probable_prime(candidate):
                primes.append(candidate)
        p, q = primes
        phi = (p - 1) * (q - 1)
        if p != q and phi % 65537:
            return p * q, 65537, pow(65537, -1, phi)


def der_name(*pairs):
    return main.der(0x30, b"".join(main.der(0x31, main.der(0x30, main.der_oid(oid) + main.der(0x0C, value.encode()))) for oid, value in pairs))


def der_time(moment):
    if moment.year < 2050:
        return main.der(0x17, moment.strftime("%y%m%d%H%M%SZ").encode())
    return main.der(0x18, moment.strftime("%Y%m%d%H%M%SZ").encode())


def der_extension(oid, value, critical=False):
    return main.der(0x30, main.der_oid(oid) + (main.der(0x01, b"\xff") if critical else b"") + main.der(0x04, value))


def basic_constraints(ca):
    return der_extension("2.5.29.19", main.der(0x30, main.der(0x01, b"\xff") if ca else b""), critical=True)


def dns_names(*names):
    return der_extension("2.5.29.17", main.der(0x30, b"".join(main.der(0x82, n.encode()) for n in names)))


def make_cert(subject, issuer, public, signer, serial, not_before, not_after, extensions=(), version=3):
    n, e = public
    algorithm = main.der(0x30, main.der_oid("1.2.840.113549.1.1.11") + main.der(0x05, b""))
    spki = main.der(0x30, main.der(0x30, main.der_oid(main.RSA_OID) + main.der(0x05, b"")) + main.der(0x03, b"\x00" + main.der(0x30, main.der_int(n) + main.der_int(e))))
    body = (main.der(0xA0, main.der_int(2)) if version == 3 else b"") + main.der_int(serial) + algorithm + issuer + main.der(0x30, der_time(not_before) + der_time(not_after)) + subject + spki
    if extensions:
        body += main.der(0xA3, main.der(0x30, b"".join(extensions)))
    tbs = main.der(0x30, body)
    signer_n, _, signer_d = signer
    k = (signer_n.bit_length() + 7) // 8
    t = main.digest_info("sha256", hashlib.sha256(tbs).digest())
    encoded = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    signature = pow(int.from_bytes(encoded, "big"), signer_d, signer_n).to_bytes(k, "big")
    return main.parse_certificate(main.der(0x30, tbs + algorithm + main.der(0x03, b"\x00" + signature)))


START = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
END = dt.datetime(2036, 1, 1, tzinfo=dt.timezone.utc)


class TinyCA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root_key, cls.leaf_key, cls.old_key = rsa_key(), rsa_key(), rsa_key()
        cls.root_name = der_name(("2.5.4.10", "Tiny"), (main.CN_OID, "Tiny Root"))
        cls.root = make_cert(cls.root_name, cls.root_name, cls.root_key[:2], cls.root_key, 1, START, END, [basic_constraints(True)])

    def leaf(self, extensions, subject_cn="svc.tiny.test", not_after=END):
        return make_cert(der_name((main.CN_OID, subject_cn)), self.root_name, self.leaf_key[:2], self.root_key, secrets.randbits(63), START, not_after, extensions)

    def test_a_certificate_signed_with_pow_verifies(self):
        leaf = self.leaf([basic_constraints(False), dns_names("svc.tiny.test")])
        verdict = main.verify([leaf], [self.root], policy=main.Policy(at=NOW, host="svc.tiny.test"))
        self.assertTrue(verdict.ok, [e.openssl_line() for e in verdict.errors])
        self.assertEqual(verdict.host_match, "DNS:svc.tiny.test")

    def test_unknown_critical_extension_is_error_34(self):
        leaf = self.leaf([basic_constraints(False), dns_names("svc.tiny.test"), der_extension("1.3.6.1.4.1.55555.1", main.der(0x05, b""), critical=True)])
        verdict = main.verify([leaf], [self.root], policy=main.Policy(at=NOW))
        self.assertEqual([(e.code, e.depth) for e in verdict.errors], [(34, 0)])

    def test_cn_is_only_used_when_asked_and_a_missing_san_warns(self):
        leaf = self.leaf([basic_constraints(False)], subject_cn="legacy.tiny.test")
        strict = main.verify([leaf], [self.root], policy=main.Policy(at=NOW, host="legacy.tiny.test"))
        self.assertEqual(strict.code, 62)
        self.assertIn("ignored unless --cn-fallback", strict.errors[0].detail)
        self.assertTrue(any("no subjectAltName" in w for w in strict.warnings))
        lenient = main.verify([leaf], [self.root], policy=main.Policy(at=NOW, host="legacy.tiny.test", cn_fallback=True))
        self.assertTrue(lenient.ok)

    def test_generalized_time_after_2049(self):
        late = dt.datetime(2061, 3, 4, 5, 6, 7, tzinfo=dt.timezone.utc)
        leaf = self.leaf([basic_constraints(False), dns_names("svc.tiny.test")], not_after=late)
        self.assertEqual(leaf.not_after, late)

    def test_trusted_first_ignores_an_expired_cross_signed_path(self):
        old_name = der_name((main.CN_OID, "Old Root"))
        expired = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
        cross = make_cert(self.root_name, old_name, self.root_key[:2], self.old_key, 7, START.replace(year=2020), expired, [basic_constraints(True)])
        leaf = self.leaf([basic_constraints(False), dns_names("svc.tiny.test")])
        verdict = main.verify([leaf, cross], [self.root], policy=main.Policy(at=NOW))
        self.assertTrue(verdict.ok, [e.openssl_line() for e in verdict.errors])
        self.assertEqual(verdict.chain[-1].fingerprint, self.root.fingerprint)

    def test_version_1_self_signed_root_still_acts_as_a_ca(self):
        v1_name = der_name((main.CN_OID, "V1 Root"))
        v1_root = make_cert(v1_name, v1_name, self.old_key[:2], self.old_key, 9, START, END, version=1)
        leaf = make_cert(der_name((main.CN_OID, "svc.tiny.test")), v1_name, self.leaf_key[:2], self.old_key, 10, START, END, [basic_constraints(False), dns_names("svc.tiny.test")])
        self.assertEqual(v1_root.version, 1)
        self.assertTrue(main.verify([leaf], [v1_root], policy=main.Policy(at=NOW)).ok)


class RecordAndHelloTests(unittest.TestCase):
    def test_openssl_client_hello_offers_sni_alpn_and_a_hybrid_key_share(self):
        flight = main.describe_flight(capture("clienthello-openssl.hex"))
        hello = flight["hellos"][0]
        self.assertEqual(flight["handshake"], ["client_hello"])
        self.assertEqual(hello["sni"], "app.cc.test")
        self.assertEqual(hello["alpn"], ["h2", "http/1.1"])
        self.assertEqual(hello["versions"], ["TLS 1.3", "TLS 1.2"])
        self.assertEqual(hello["legacy_version"], "TLS 1.2")
        self.assertEqual(hello["key_shares"], [{"group": "X25519MLKEM768", "bytes": 1216}, {"group": "x25519", "bytes": 32}])
        self.assertEqual(hello["cipher_suites"][:3], ["TLS_AES_256_GCM_SHA384", "TLS_CHACHA20_POLY1305_SHA256", "TLS_AES_128_GCM_SHA256"])

    def test_curl_client_hello_and_its_ja3(self):
        hello = main.describe_flight(capture("clienthello-curl.hex"))["hellos"][0]
        self.assertEqual((hello["sni"], hello["alpn"]), ("app.cc.test", ["h2", "http/1.1"]))
        self.assertEqual(hello["versions"], ["TLS 1.3", "TLS 1.2", "TLS 1.1", "TLS 1.0"])
        self.assertFalse(hello["grease"])
        self.assertTrue(hello["ja3"].startswith("771,4867-4866-4865-"))
        self.assertEqual(hello["ja3_md5"], hashlib.md5(hello["ja3"].encode()).hexdigest())

    def test_grease_values_are_recognised(self):
        self.assertTrue(all(main.is_grease(v) for v in (0x0A0A, 0x1A1A, 0xFAFA)))
        self.assertFalse(any(main.is_grease(v) for v in (0x0A1A, 0x1301, 0x001D)))
        self.assertEqual(main.label(main.GROUPS, 0x2A2A), "GREASE")

    def test_tls12_server_flight_carries_the_chain_in_clear(self):
        data = capture("server-tls12.hex")
        flight = main.describe_flight(data)
        hello = flight["hellos"][0]
        self.assertEqual(flight["handshake"], ["server_hello", "certificate", "server_key_exchange", "server_hello_done"])
        self.assertEqual((hello["version"], hello["version_from"]), ("TLS 1.2", "legacy_version"))
        self.assertEqual(hello["cipher_suite"], "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256")
        self.assertEqual(hello["alpn"], "h2")
        self.assertIn("also speaks TLS 1.3", hello["downgrade_sentinel"])
        self.assertEqual(flight["key_exchange_group"], "x25519")
        chain = main.flight_certificates(data)
        self.assertEqual([c.fingerprint for c in chain], [cert("leaf.pem").fingerprint, cert("inter.pem").fingerprint])

    def test_tls13_server_hello_hides_alpn_and_certificates(self):
        flight = main.describe_flight(capture("server-tls13.hex"))
        hello = flight["hellos"][0]
        self.assertEqual((hello["version"], hello["version_from"]), ("TLS 1.3", "supported_versions"))
        self.assertEqual(hello["cipher_suite"], "TLS_AES_256_GCM_SHA384")
        self.assertEqual(hello["key_share"], "X25519MLKEM768")
        self.assertIsNone(hello["alpn"])
        self.assertEqual(flight["certificates"], [])
        self.assertEqual(flight["change_cipher_spec"], 1)
        self.assertGreater(flight["encrypted_records"], 0)
        self.assertIn("EncryptedExtensions", main.render_flight(flight))

    def test_hello_retry_request_costs_a_second_client_hello(self):
        server = main.describe_flight(capture("server-hrr.hex"))["hellos"]
        self.assertEqual([h["type"] for h in server], ["hello_retry_request", "server_hello"])
        self.assertEqual(server[0]["key_share"], "secp384r1")
        client = main.describe_flight(capture("client-hrr.hex"))["hellos"]
        self.assertEqual([k["group"] for k in client[0]["key_shares"]], ["X25519MLKEM768", "x25519"])
        self.assertEqual(client[1]["key_shares"], [{"group": "secp384r1", "bytes": 97}])

    def test_a_handshake_message_split_across_records_is_reassembled(self):
        record = capture("clienthello-openssl.hex")
        fragment = record[5:]
        split = b"".join(b"\x16\x03\x01" + main.vec(2, part) for part in (fragment[:300], fragment[300:]))
        whole = main.describe_flight(record)["hellos"][0]
        again = main.describe_flight(split)
        self.assertEqual(again["records"], ["handshake", "handshake"])
        self.assertEqual(again["hellos"][0], whole)

    def test_a_partial_record_waits_for_more_bytes(self):
        flight = main.read_flight(capture("clienthello-openssl.hex")[:100])
        self.assertEqual((flight.records, flight.messages, flight.leftover), ([], [], 100))
        self.assertFalse(main.flight_finished(capture("clienthello-openssl.hex")[:100], (1,)))

    def test_plaintext_http_on_a_tls_port_is_named(self):
        with self.assertRaisesRegex(main.TlsError, "plaintext HTTP"):
            main.parse_records(b"GET / HTTP/1.1\r\nHost: app\r\n\r\n")
        with self.assertRaisesRegex(main.TlsError, "not a TLS record type"):
            main.parse_records(b"\x42" * 10)

    def test_alert_record_and_probe_hello_round_trip(self):
        alert = main.describe_flight(main.alert_record(40))["alerts"]
        self.assertEqual(alert, [{"level": "fatal", "description": "handshake_failure", "code": 40}])
        hello = main.describe_flight(main.build_client_hello("app.cc.test", ["h2", "http/1.1"], bytes(32)))["hellos"][0]
        self.assertEqual((hello["sni"], hello["alpn"], hello["versions"]), ("app.cc.test", ["h2", "http/1.1"], ["TLS 1.2"]))
        self.assertEqual(len(hello["cipher_suites"]), len(main.PROBE_SUITES))
        by_ip = main.describe_flight(main.build_client_hello("127.0.0.1", [], bytes(32)))["hellos"][0]
        self.assertIsNone(by_ip["sni"])

    def test_duplicate_extensions_are_rejected(self):
        twice = main.tls_extension(23, b"") * 2
        body = b"\x03\x03" + bytes(32) + b"\x00" + main.vec(2, b"\x13\x01") + b"\x01\x00" + main.vec(2, twice)
        with self.assertRaisesRegex(main.TlsError, "appears twice"):
            main.parse_client_hello(body)


class SocketTests(unittest.TestCase):
    def serve(self, handler):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def run():
            with contextlib.suppress(OSError):
                conn, _ = listener.accept()
                with conn:
                    handler(conn)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        return listener.getsockname()[1]

    def fake_tls_server(self, reply, hold_open=True):
        seen = {}

        def handler(conn):
            seen["hello"] = main.read_client_hello(conn, 5)
            conn.sendall(reply)
            if hold_open:
                conn.settimeout(5)
                with contextlib.suppress(OSError):
                    conn.recv(1)

        return self.serve(handler), seen

    def test_listener_records_what_a_python_ssl_client_sends(self):
        listener = main.make_listener("127.0.0.1", 0)
        self.addCleanup(listener.close)
        result = {}
        thread = threading.Thread(target=lambda: result.update(bytes=main.capture(listener, timeout=5, wait=5)), daemon=True)
        thread.start()
        context = ssl.create_default_context()
        context.set_alpn_protocols(["h2", "http/1.1"])
        with socket.create_connection(listener.getsockname()[:2], timeout=5) as raw:
            with self.assertRaises((ssl.SSLError, ConnectionError)):
                context.wrap_socket(raw, server_hostname="app.cc.test").do_handshake()
        thread.join(5)
        hello = main.describe_flight(result["bytes"][0])["hellos"][0]
        self.assertEqual((hello["type"], hello["sni"], hello["alpn"]), ("client_hello", "app.cc.test", ["h2", "http/1.1"]))

    def test_relay_records_both_directions(self):
        reply = capture("server-tls12.hex")
        upstream, seen = self.fake_tls_server(reply, hold_open=False)
        listener = main.make_listener("127.0.0.1", 0)
        self.addCleanup(listener.close)
        result = {}
        thread = threading.Thread(target=lambda: result.update(pair=main.capture(listener, ("127.0.0.1", upstream), timeout=5, wait=5)), daemon=True)
        thread.start()
        hello = capture("clienthello-openssl.hex")
        received = b""
        with socket.create_connection(listener.getsockname()[:2], timeout=5) as client:
            client.sendall(hello)
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                received += chunk
        thread.join(5)
        self.assertEqual(result["pair"], (hello, reply))
        self.assertEqual((received, seen["hello"]), (reply, hello))

    def test_probe_stops_at_server_hello_done_and_reads_the_chain(self):
        port, seen = self.fake_tls_server(capture("server-tls12.hex"))
        data = main.probe("127.0.0.1", port, "app.cc.test", ["h2"], timeout=5)
        self.assertEqual(main.describe_flight(seen["hello"])["hellos"][0]["sni"], "app.cc.test")
        self.assertEqual(len(main.flight_certificates(data)), 2)

    def test_verify_connect_builds_the_chain_from_the_probe(self):
        port, _ = self.fake_tls_server(capture("server-tls12.hex"))
        code, out, err = run_cli("verify", "--connect", f"127.0.0.1:{port}", "--servername", "app.cc.test", "--CAfile", pem("root.pem"), "--at", "2026-11-01", "--json")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual((report["ok"], report["host"], report["host_match"]), (True, "app.cc.test", "DNS:app.cc.test"))
        self.assertEqual(len(report["chain"]), 3)

    def test_a_tls13_only_server_answers_the_probe_with_an_alert(self):
        port, _ = self.fake_tls_server(main.alert_record(70))
        code, _, err = run_cli("verify", "--connect", f"127.0.0.1:{port}", "--CAfile", pem("root.pem"))
        self.assertEqual(code, 2)
        self.assertIn("protocol_version", err)

    def test_connection_refused_is_an_input_error(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaisesRegex(main.InputError, "cannot connect"):
            main.probe("127.0.0.1", port, None, [], timeout=2)


class DerTests(unittest.TestCase):
    def test_der_reader_rejects_ber_and_truncation(self):
        with self.assertRaisesRegex(main.DerError, "indefinite length"):
            main.read_tlv(b"\x30\x80\x00\x00")
        with self.assertRaisesRegex(main.DerError, "runs past the end"):
            main.read_tlv(b"\x04\x05abc")
        with self.assertRaisesRegex(main.DerError, "well-formed"):
            main.parse_certificate(main.der(0x30, main.der(0x30, b"") + main.der(0x30, b"") + main.der(0x03, b"\x00")))

    def test_oids_round_trip(self):
        for oid in ("1.2.840.113549.1.1.11", "2.5.29.17", "1.3.6.1.4.1.11129.2.4.2", "2.999.3"):
            tlv, _ = main.read_tlv(main.der_oid(oid))
            self.assertEqual(main.decode_oid(tlv.value), oid)

    def test_utctime_pivots_at_1950_and_generalized_time_has_four_digit_years(self):
        decode = lambda tag, text: main.decode_time(main.read_tlv(main.der(tag, text))[0])
        self.assertEqual(decode(0x17, b"491231235959Z").year, 2049)
        self.assertEqual(decode(0x17, b"500101000000Z").year, 1950)
        self.assertEqual(decode(0x18, b"99991231235959Z").year, 9999)
        with self.assertRaisesRegex(main.DerError, "not UTCTime"):
            decode(0x17, b"2601010000Z")

    def test_fields_match_recorded_openssl_x509(self):
        with open(os.path.join(FIXTURES, "openssl-x509.json")) as handle:
            recorded = json.load(handle)["certificates"]
        for entry in recorded:
            with self.subTest(entry["file"]):
                fields = dict(line.split("=", 1) for line in entry["fields"])
                summary = main.certificate_summary(cert(entry["file"]))
                self.assertEqual(int(fields["serial"], 16), int(summary["serial"], 16))
                self.assertEqual(fields["sha256 Fingerprint"], summary["sha256_fingerprint"])
                self.assertEqual((fields["subject"], fields["issuer"]), (summary["subject_rfc4514"], summary["issuer_rfc4514"]))
                for key in ("notBefore", "notAfter"):
                    moment = dt.datetime.strptime(fields[key], "%b %d %H:%M:%S %Y GMT").strftime("%Y-%m-%dT%H:%M:%SZ")
                    self.assertEqual(moment, summary["not_before" if key == "notBefore" else "not_after"])
                lines = entry["extensions"]
                san = lines[lines.index("X509v3 Subject Alternative Name: ") + 1].split(", ") if "X509v3 Subject Alternative Name: " in lines else []
                ours = [f"DNS:{n}" for n in summary["dns_names"]] + [f"IP Address:{a}" for a in summary["ip_addresses"]]
                self.assertEqual([s.strip() for s in san], ours)
                self.assertEqual("CA:TRUE" in "\n".join(lines), bool(summary["is_ca"]))

    def test_leaf_extensions(self):
        leaf, inter = cert("leaf.pem"), cert("inter.pem")
        self.assertEqual((leaf.version, leaf.is_ca, leaf.path_len), (3, False, None))
        self.assertEqual(leaf.key_usage, ["digitalSignature", "keyEncipherment"])
        self.assertEqual(leaf.ext_key_usage, ["serverAuth"])
        self.assertEqual(leaf.authority_key_id, inter.subject_key_id)
        self.assertEqual((inter.is_ca, inter.path_len), (True, 0))
        self.assertEqual(main.certificate_summary(leaf)["critical"], ["basicConstraints", "keyUsage"])

    def test_names_render_escape_and_compare(self):
        name = main.Name([[("2.5.4.6", "DE")], [("2.5.4.10", "Acme, Inc.")], [("2.5.4.3", "a+b"), ("2.5.4.11", "Ops")]])
        self.assertEqual(name.text(), "C=DE, O=Acme, Inc., CN=a+b + OU=Ops")
        self.assertEqual(name.rfc4514(), "CN=a\\+b+OU=Ops,O=Acme\\, Inc.,C=DE")
        other = main.Name([[("2.5.4.6", "de")], [("2.5.4.10", "ACME,   Inc.")], [("2.5.4.11", "ops"), ("2.5.4.3", "A+B")]])
        self.assertEqual(name.key(), other.key())

    def test_bmp_teletex_and_unknown_string_types(self):
        decode = lambda tag, raw: main.decode_string(main.read_tlv(main.der(tag, raw))[0])
        self.assertEqual(decode(0x1E, "Zürich".encode("utf-16-be")), "Zürich")
        self.assertEqual(decode(0x14, "Zürich".encode("latin-1")), "Zürich")
        self.assertEqual(decode(0x04, b"\x01"), "#040101")

    def test_compressed_ec_point_decodes_to_the_same_key(self):
        key = cert("ec-leaf.pem").public_key
        x, y = key.point
        compressed = bytes([2 + (y & 1)]) + x.to_bytes(32, "big")
        self.assertEqual(main.decode_point(main.P256, compressed), (x, y))
        with self.assertRaisesRegex(main.DerError, "not a point"):
            main.decode_point(main.P256, b"\x04" + (x + 1).to_bytes(32, "big") + y.to_bytes(32, "big"))

    def test_pem_noise_der_and_keys(self):
        leaf, inter = read(pem("leaf.pem")), read(pem("inter.pem"))
        showcerts = b"CONNECTED(00000003)\ndepth=0 CN = app.cc.test\n---\n" + leaf + b" 1 s:CN=...\n" + inter + b"---\nVerify return code: 0 (ok)\n"
        self.assertEqual(len(main.load_certificates(showcerts, "showcerts.txt")), 2)
        self.assertEqual(main.load_certificates(cert("leaf.pem").der, "leaf.der")[0].serial, 0x1002)
        with self.assertRaisesRegex(main.InputError, "private key or a CSR"):
            main.load_certificates(b"-----BEGIN %s-----\nAAAA\n-----END %s-----\n" % (b"PRIVATE KEY", b"PRIVATE KEY"), "key.pem")


class SignatureTests(unittest.TestCase):
    def test_rsa_pkcs1_v15_is_pow_and_a_byte_compare(self):
        leaf, inter = cert("leaf.pem"), cert("inter.pem")
        n, e = inter.public_key.rsa
        self.assertTrue(main.rsa_pkcs1_verify(n, e, leaf.signature, "sha256", leaf.tbs))
        tampered = leaf.tbs[:-1] + bytes([leaf.tbs[-1] ^ 1])
        self.assertFalse(main.rsa_pkcs1_verify(n, e, leaf.signature, "sha256", tampered))
        self.assertFalse(main.rsa_pkcs1_verify(n, e, leaf.signature[1:], "sha256", leaf.tbs))

    def test_digest_info_matches_rfc8017(self):
        self.assertEqual(main.digest_info("sha256", bytes(32))[:19].hex(), "3031300d060960864801650304020105000420")
        self.assertEqual(main.digest_info("sha1", bytes(20))[:15].hex(), "3021300906052b0e03021a05000414")

    def test_ecdsa_p256_rfc6979_sample_vector(self):
        public = (0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6, 0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299)
        r = 0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716
        s = 0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8
        digest = hashlib.sha256(b"sample").digest()
        self.assertTrue(main.ecdsa_verify(main.P256, public, digest, r, s))
        self.assertFalse(main.ecdsa_verify(main.P256, public, hashlib.sha256(b"test").digest(), r, s))
        self.assertFalse(main.ecdsa_verify(main.P256, public, digest, r, main.P256.n))

    def test_curve_parameters_are_consistent(self):
        for curve in (main.P256, main.P384):
            with self.subTest(curve.name):
                self.assertTrue(main.on_curve(curve, curve.generator))
                self.assertIsNone(main.scalar_mult(curve, curve.n, curve.generator))

    def test_p384_intermediate_signs_a_p256_leaf_with_sha384(self):
        leaf, inter = cert("ec-leaf.pem"), cert("ec-inter.pem")
        self.assertEqual((leaf.signature_name, inter.public_key.curve_name, leaf.public_key.curve_name), ("ecdsa-with-SHA384", "P-384", "P-256"))
        self.assertIsNone(main.signature_problem(leaf, inter))
        self.assertEqual(main.signature_problem(leaf, cert("inter.pem"))[0], 7)

    def test_a_tampered_certificate_fails_with_error_7(self):
        original = cert("leaf.pem").der
        forged = original.replace(b"app.cc.test", b"apq.cc.test", 1)
        verdict = main.verify(main.load_certificates(forged, "forged.der"), [cert("root.pem")], [cert("inter.pem")], main.Policy(at=NOW))
        self.assertEqual([(e.code, e.depth) for e in verdict.errors], [(7, 0)])


class VerifyTests(unittest.TestCase):
    def test_first_error_matches_recorded_openssl_verify_for_every_case(self):
        with open(os.path.join(FIXTURES, "openssl-verify.json")) as handle:
            cases = json.load(handle)["cases"]
        self.assertGreaterEqual(len(cases), 15)
        for case in cases:
            with self.subTest(case["name"]):
                policy = main.Policy(at=dt.datetime.fromtimestamp(case["at"], dt.timezone.utc), host=case["host"], partial_chain=case["partial_chain"])
                verdict = main.verify([cert(case["leaf"])], [cert(n) for n in case["cafile"]], [cert(n) for n in case["untrusted"]], policy)
                errors = [re.match(r"error (\d+) at (\d+) depth lookup", line) for line in case["openssl_output"]]
                errors = [(int(m.group(1)), int(m.group(2))) for m in errors if m]
                ours = [(e.code, e.depth) for e in verdict.errors]
                self.assertEqual(ours[:1], errors[:1], case["openssl_output"])
                if errors:
                    self.assertIn(verdict.errors[0].openssl_line().split(":")[0], "\n".join(case["openssl_output"]))

    def test_a_lone_leaf_reports_20_then_21_like_s_client(self):
        verdict = main.verify([cert("leaf.pem")], [cert("root.pem")], policy=main.Policy(at=NOW))
        self.assertEqual([e.code for e in verdict.errors], [20, 21])
        self.assertIn("CloudCaptain Lab Intermediate", verdict.errors[0].detail)

    def test_hostname_rules(self):
        table = [
            ("app.cc.test", "APP.cc.test.", True),
            ("*.api.cc.test", "orders.api.cc.test", True),
            ("*.api.cc.test", "api.cc.test", False),
            ("*.api.cc.test", "eu.orders.api.cc.test", False),
            ("f*.api.cc.test", "foo.api.cc.test", False),
            ("*.test", "cc.test", False),
            ("*", "localhost", False),
            ("api.*.test", "api.cc.test", False),
        ]
        for pattern, host, expected in table:
            with self.subTest(pattern=pattern, host=host):
                self.assertEqual(main.hostname_matches(pattern, host), expected)

    def test_ip_addresses_match_only_ip_sans(self):
        leaf = cert("leaf.pem")
        self.assertEqual(main.match_host(leaf, "127.0.0.1")[0], "IP:127.0.0.1")
        self.assertEqual(main.match_host(leaf, "[::1]")[1].code, 64)

    def test_purpose_any_skips_the_server_auth_check(self):
        policy = main.Policy(at=NOW, purpose="any")
        self.assertTrue(main.verify([cert("client-only.pem")], [cert("root.pem")], [cert("inter.pem")], policy).ok)

    def test_expiry_soon_is_a_warning_not_an_error(self):
        verdict = main.verify([cert("leaf.pem")], [cert("root.pem")], [cert("inter.pem")], main.Policy(at=dt.datetime(2026, 12, 15, tzinfo=dt.timezone.utc)))
        self.assertTrue(verdict.ok)
        self.assertIn("expires in 17 days", " ".join(verdict.warnings))


class CliTests(unittest.TestCase):
    def test_verify_exit_codes_and_openssl_wording(self):
        code, out, _ = run_cli("verify", pem("expired.pem"), "--untrusted", pem("inter.pem"), "--CAfile", pem("root.pem"), "--at", "@1793491200")
        self.assertEqual(code, 1)
        self.assertIn("error 10 at 0 depth lookup: certificate has expired", out)
        code, out, _ = run_cli("verify", pem("leaf.pem"), "--untrusted", pem("inter.pem"), "--CAfile", pem("root.pem"), "--host", "app.cc.test", "--at", "2026-11-01")
        self.assertEqual(code, 0)
        self.assertTrue(out.rstrip().endswith("OK"))
        code, _, err = run_cli("verify", os.path.join(FIXTURES, "missing.pem"), "--CAfile", pem("root.pem"))
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_verify_json_shape(self):
        code, out, _ = run_cli("verify", pem("wrong-san.pem"), "--untrusted", pem("inter.pem"), "--CAfile", pem("root.pem"), "--host", "app.cc.test", "--at", "2026-11-01", "--json")
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual((report["verify_code"], report["verify_message"]), (62, "hostname mismatch"))
        self.assertEqual(report["chain"][0]["dns_names"], ["other.cc.test"])
        self.assertIn("other.cc.test", report["errors"][0]["detail"])

    def test_x509_and_hello_commands(self):
        code, out, _ = run_cli("x509", pem("leaf.pem"), pem("inter.pem"), "--json")
        self.assertEqual(code, 0)
        self.assertEqual([c["serial"] for c in json.loads(out)["certificates"]], ["1002", "1001"])
        code, out, _ = run_cli("hello", os.path.join(FIXTURES, "clienthello-curl.hex"))
        self.assertEqual(code, 0)
        self.assertIn("sni            app.cc.test", out)
        with tempfile.NamedTemporaryFile("wb", suffix=".bin", delete=False) as handle:
            handle.write(capture("server-tls13.hex"))
        self.addCleanup(os.unlink, handle.name)
        code, out, _ = run_cli("hello", handle.name, "--json")
        self.assertEqual(json.loads(out)["hellos"][0]["version"], "TLS 1.3")

    def test_times_and_addresses(self):
        self.assertEqual(main.parse_at("@1793491200"), NOW)
        self.assertEqual(main.parse_at("2026-11-01"), NOW)
        self.assertEqual(main.parse_at("2026-11-01T00:00:00Z"), NOW)
        with self.assertRaises(main.InputError):
            main.parse_at("next tuesday")
        self.assertEqual(main.split_address("[::1]:8443"), ("::1", 8443))
        self.assertEqual(main.split_address("example.com"), ("example.com", 443))
        self.assertEqual(main.split_address("127.0.0.1:4433"), ("127.0.0.1", 4433))


if __name__ == "__main__":
    unittest.main()
