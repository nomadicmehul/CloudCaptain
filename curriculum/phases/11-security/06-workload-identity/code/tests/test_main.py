import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
RFC6979_KEY = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
WEB = main.parse_spiffe_id("spiffe://cc.test/web")
API = main.parse_spiffe_id("spiffe://cc.test/api")
BATCH = main.parse_spiffe_id("spiffe://cc.test/batch")
OTHER_WEB = main.parse_spiffe_id("spiffe://other.test/web")


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class SpiffeIdTests(unittest.TestCase):
    def test_valid_ids_split_into_trust_domain_and_path(self):
        spiffe_id = main.parse_spiffe_id("spiffe://prod.cc.test/ns/shop/sa/web-1.v2_x")
        self.assertEqual((spiffe_id.trust_domain, spiffe_id.path), ("prod.cc.test", "/ns/shop/sa/web-1.v2_x"))
        self.assertEqual(spiffe_id.segments, ["ns", "shop", "sa", "web-1.v2_x"])
        self.assertEqual(str(spiffe_id), "spiffe://prod.cc.test/ns/shop/sa/web-1.v2_x")
        self.assertEqual(main.parse_spiffe_id("spiffe://cc.test").path, "")
        self.assertEqual(main.parse_spiffe_id("spiffe://10.0.0.1/x").trust_domain, "10.0.0.1")

    def test_each_rule_of_the_specification_rejects_an_id(self):
        cases = {
            "spiffe://CC.test/web": "lowercase",
            "SPIFFE://cc.test/web": "scheme",
            "https://cc.test/web": "scheme",
            "spiffe:///web": "empty trust domain",
            "spiffe://cc.test/web/": "ends with",
            "spiffe://cc.test//web": "empty",
            "spiffe://cc.test/a/../b": "'..'",
            "spiffe://cc.test/./b": "'.'",
            "spiffe://cc.test/web?x=1": "query",
            "spiffe://cc.test/web#frag": "fragment",
            "spiffe://cc.test/w%20b": "percent",
            "spiffe://cc.test:443/web": "port",
            "spiffe://user@cc.test/web": "userinfo",
            "spiffe://cc.test/web*": "segment",
            "spiffe://cc.test/" + "a" * 2040: "2048",
        }
        for text, words in cases.items():
            with self.subTest(text=text[:40]):
                with self.assertRaises(main.SpiffeIdError) as caught:
                    main.parse_spiffe_id(text)
                self.assertIn(words, str(caught.exception))

    def test_trust_domains_accept_both_spellings_but_not_a_workload(self):
        self.assertEqual(main.parse_trust_domain("cc.test"), "cc.test")
        self.assertEqual(main.parse_trust_domain("spiffe://cc.test"), "cc.test")
        with self.assertRaises(main.SpiffeIdError):
            main.parse_trust_domain("spiffe://cc.test/web")
        with self.assertRaises(main.SpiffeIdError):
            main.parse_trust_domain("CC.TEST")


class DerTests(unittest.TestCase):
    def test_lengths_integers_and_oids_encode_like_x690(self):
        self.assertEqual(main.der_length(127).hex(), "7f")
        self.assertEqual(main.der_length(128).hex(), "8180")
        self.assertEqual(main.der_length(256).hex(), "820100")
        self.assertEqual(main.der_int(0).hex(), "020100")
        self.assertEqual(main.der_int(127).hex(), "02017f")
        self.assertEqual(main.der_int(128).hex(), "02020080")
        self.assertEqual(main.der_oid("1.2.840.10045.2.1").hex(), "06072a8648ce3d0201")
        self.assertEqual(main.der_oid("2.5.29.17").hex(), "0603551d11")
        for oid in ("1.2.840.10045.3.1.7", "1.3.6.1.5.5.7.3.2", "2.999.3"):
            self.assertEqual(main.decode_oid(main.der_oid(oid)[2:]), oid)

    def test_key_usage_bit_strings_drop_trailing_zero_bits(self):
        self.assertEqual(main.der_bits([0]).hex(), "03020780")
        self.assertEqual(main.der_bits([5, 6]).hex(), "03020106")
        self.assertEqual(main.der_bits([0, 2, 4]).hex(), "030203a8")
        self.assertEqual(main.der_bits([8]).hex(), "0303070080")

    def test_times_switch_to_generalized_time_in_2050(self):
        self.assertEqual(main.der_time(NOW).hex(), "170d" + b"261102100000Z".hex())
        late = dt.datetime(2050, 1, 1, tzinfo=UTC)
        self.assertEqual(main.der_time(late)[0], 0x18)
        for moment in (NOW, late, dt.datetime(1999, 12, 31, 23, 59, 59, tzinfo=UTC)):
            item, _ = main.read_tlv(main.der_time(moment))
            self.assertEqual(main.decode_time(item), moment)

    def test_the_reader_rejects_ber_and_truncation(self):
        with self.assertRaisesRegex(main.DerError, "indefinite"):
            main.read_tlv(bytes.fromhex("3080"))
        with self.assertRaisesRegex(main.DerError, "runs past"):
            main.read_tlv(bytes.fromhex("300500"))
        with self.assertRaises(main.DerError):
            main.parse_certificate(b"\x30\x03\x02\x01\x01")


class EcdsaTests(unittest.TestCase):
    def setUp(self):
        self.key = main.ec_key(RFC6979_KEY)

    def test_public_key_matches_rfc6979_appendix_a_2_5(self):
        self.assertEqual(self.key.public, (0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6, 0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299))

    def test_nonces_and_signatures_match_the_published_p256_sha256_vectors(self):
        vectors = [
            (b"sample", 0xA6E3C57DD01ABE90086538398355DD4C3B17AA873382B0F24D6129493D8AAD60,
             0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716,
             0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8),
            (b"test", 0xD16B6AE827F17175E040871A1C7EC3500192C4C92677336EC2537ACAEE0008E0,
             0xF1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367,
             0x019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083),
        ]
        for message, k, r, s in vectors:
            digest = hashlib.sha256(message).digest()
            self.assertEqual(next(main.rfc6979_nonces(main.P256, self.key.d, digest)), k)
            self.assertEqual(main.ecdsa_sign(self.key, digest), (r, s))
            self.assertTrue(main.ecdsa_verify(main.P256, self.key.public, digest, r, s))

    def test_tampering_with_message_signature_or_key_fails(self):
        digest = hashlib.sha256(b"spiffe://cc.test/web").digest()
        r, s = main.ecdsa_sign(self.key, digest)
        other = main.generate_key()
        self.assertTrue(main.ecdsa_verify(main.P256, self.key.public, digest, r, s))
        self.assertFalse(main.ecdsa_verify(main.P256, self.key.public, hashlib.sha256(b"spiffe://cc.test/admin").digest(), r, s))
        self.assertFalse(main.ecdsa_verify(main.P256, self.key.public, digest, r, (s + 1) % main.P256.n))
        self.assertFalse(main.ecdsa_verify(main.P256, other.public, digest, r, s))
        self.assertFalse(main.ecdsa_verify(main.P256, self.key.public, digest, r, 0))
        self.assertEqual(main.parse_der_signature(main.der_signature(r, s)), (r, s))

    def test_curve_parameters_are_consistent(self):
        for curve in (main.P256, main.P384):
            with self.subTest(curve.name):
                self.assertTrue(main.on_curve(curve, curve.generator))
                self.assertIsNone(main.scalar_mult(curve, curve.n, curve.generator))


class Pki(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cc = main.new_authority("cc.test", NOW)
        cls.other = main.new_authority("other.test", NOW)
        cls.web = main.issue_svid(cls.cc, WEB, NOW, 300)
        cls.bundles = main.bundle_set([cls.cc.bundle(), cls.other.bundle()])


class CertificateTests(Pki):
    def test_ca_and_leaf_carry_the_x509_svid_extensions(self):
        ca, leaf = self.cc.cert, self.web.leaf
        self.assertTrue(ca.is_ca)
        self.assertEqual(ca.key_usage, ["keyCertSign", "cRLSign"])
        self.assertEqual(ca.uris, ["spiffe://cc.test"])
        self.assertFalse(leaf.is_ca)
        self.assertEqual(leaf.key_usage, ["digitalSignature"])
        self.assertEqual(leaf.ext_key_usage, [main.OID_SERVER_AUTH, main.OID_CLIENT_AUTH])
        self.assertEqual(leaf.uris, ["spiffe://cc.test/web"])
        self.assertEqual(leaf.authority_key_id, ca.subject_key_id)
        self.assertEqual(leaf.subject_key_id, hashlib.sha1(main.ec_point_bytes(main.P256, self.web.key.public)).digest())
        self.assertIn(main.OID_KEY_USAGE, leaf.critical)
        self.assertIn(main.OID_BASIC_CONSTRAINTS, leaf.critical)
        self.assertEqual(main.svid_problems(leaf), [])
        self.assertEqual(main.svid_problems(ca), [])
        self.assertEqual(main.certificate_summary(leaf)["signature_algorithm"], "ecdsa-with-SHA256")

    def test_lifetime_has_a_backdated_start_and_is_capped_by_the_ca(self):
        self.assertEqual(self.web.leaf.not_before, NOW - dt.timedelta(seconds=10))
        self.assertEqual(self.web.leaf.not_after, NOW + dt.timedelta(seconds=300))
        self.assertEqual(self.web.leaf.lifetime, 310)
        long_lived = main.issue_svid(self.cc, WEB, NOW, 10 ** 7)
        self.assertEqual(long_lived.leaf.not_after, self.cc.cert.not_after)

    def test_signatures_chain_and_a_flipped_byte_breaks_them(self):
        self.assertTrue(main.signed_by(self.web.leaf, self.cc.cert))
        self.assertTrue(main.signed_by(self.cc.cert, self.cc.cert))
        self.assertFalse(main.signed_by(self.web.leaf, self.other.cert))
        tampered = bytearray(self.web.leaf.der)
        position = tampered.find(b"spiffe://cc.test/web") + len("spiffe://cc.test/")
        tampered[position:position + 3] = b"adm"
        forged = main.parse_certificate(bytes(tampered))
        self.assertEqual(forged.uris, ["spiffe://cc.test/adm"])
        self.assertFalse(main.signed_by(forged, self.cc.cert))

    def test_pkcs8_keys_round_trip_and_match_their_certificate(self):
        text = self.web.key_pem()
        self.assertIn("-----BEGIN PRIVATE KEY-----", text)
        key = main.load_private_key(text, "key.pem")
        self.assertEqual(key, self.web.key)
        self.assertTrue(main.key_matches(key, self.web.leaf))
        self.assertFalse(main.key_matches(key, self.cc.cert))

    def test_a_ca_issues_only_for_its_own_trust_domain_and_only_workloads(self):
        with self.assertRaisesRegex(main.InputError, "not in cc.test"):
            main.issue_svid(self.cc, OTHER_WEB, NOW)
        with self.assertRaisesRegex(main.InputError, "needs a path"):
            main.issue_svid(self.cc, main.parse_spiffe_id("spiffe://cc.test"), NOW)
        impostor = main.issue_svid(self.other, WEB, NOW, any_trust_domain=True)
        self.assertEqual(impostor.leaf.uris, ["spiffe://cc.test/web"])

    def test_conformance_check_names_each_broken_rule(self):
        key = main.generate_key()
        subject = main.der_name([("2.5.4.10", "miniid")])
        san = main.der_extension(main.OID_SAN, main.der(0x30, main.der(0x86, b"spiffe://cc.test/a"), main.der(0x86, b"spiffe://cc.test/b")))
        broken = main.sign_certificate(subject, self.cc.cert.subject_der, key, self.cc.key, NOW, NOW + dt.timedelta(hours=1), [
            main.der_extension(main.OID_BASIC_CONSTRAINTS, main.der(0x30, main.der(0x01, b"\xff")), critical=True),
            main.der_extension(main.OID_KEY_USAGE, main.der_bits([0, 5])),
            main.der_extension(main.OID_EXT_KEY_USAGE, main.der(0x30, main.der_oid(main.OID_SERVER_AUTH))),
            san,
        ])
        broken.is_ca = False
        problems = " | ".join(main.svid_problems(broken))
        for words in ("2 URI SANs", "keyCertSign", "serverAuth and clientAuth", "critical"):
            self.assertIn(words, problems)
        reasons = [reason for reason, _ in main.leaf_problems(broken)]
        self.assertEqual(reasons, ["uri-san-count", "not-a-leaf"])

    @unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
    def test_openssl_verifies_the_chain_and_prints_the_extensions(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        now = main.utcnow()
        authority = main.new_authority("cc.test", now)
        svid = main.issue_svid(authority, WEB, now, 600)
        paths = {name: os.path.join(folder, name) for name in ("ca.pem", "svid.pem", "key.pem")}
        main.write_file(paths["ca.pem"], authority.cert.pem())
        main.write_file(paths["svid.pem"], svid.chain_pem())
        main.write_file(paths["key.pem"], svid.key_pem(), private=True)
        for purpose in ("sslclient", "sslserver"):
            result = subprocess.run(["openssl", "verify", "-CAfile", paths["ca.pem"], "-purpose", purpose, paths["svid.pem"]], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = subprocess.run(["openssl", "x509", "-in", paths["svid.pem"], "-noout", "-text"], capture_output=True, text=True, check=True).stdout
        for words in ("URI:spiffe://cc.test/web", "CA:FALSE", "Digital Signature", "TLS Web Client Authentication", "ecdsa-with-SHA256"):
            self.assertIn(words, text)
        cert_key = subprocess.run(["openssl", "x509", "-in", paths["svid.pem"], "-noout", "-pubkey"], capture_output=True, text=True, check=True).stdout
        private_key = subprocess.run(["openssl", "pkey", "-in", paths["key.pem"], "-pubout"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(cert_key, private_key)


class BundleTests(Pki):
    def test_spiffe_bundle_json_round_trips_and_ignores_jwt_keys(self):
        document = self.cc.bundle().to_spiffe()
        entry = document["keys"][0]
        self.assertEqual((entry["use"], entry["kty"], entry["crv"]), ("x509-svid", "EC", "P-256"))
        self.assertEqual(document["spiffe_sequence"], 1)
        document["keys"].append({"use": "jwt-svid", "kty": "EC", "kid": "abc", "crv": "P-256", "x": "AA", "y": "AA"})
        bundle = main.parse_bundle(json.dumps(document).encode(), None, "bundle.json")
        self.assertEqual(bundle.trust_domain, "cc.test")
        self.assertEqual([cert.fingerprint for cert in bundle.authorities], [self.cc.cert.fingerprint])
        with self.assertRaisesRegex(main.InputError, "no x509-svid"):
            main.parse_bundle(b'{"keys": [{"use": "jwt-svid"}]}', "cc.test", "jwt.json")

    def test_pem_bundles_take_the_trust_domain_from_the_ca_or_from_the_caller(self):
        self.assertEqual(main.parse_bundle(self.cc.cert.pem().encode(), None, "ca.pem").trust_domain, "cc.test")
        mixed = (self.cc.cert.pem() + self.other.cert.pem()).encode()
        with self.assertRaisesRegex(main.InputError, "TRUST_DOMAIN="):
            main.parse_bundle(mixed, None, "mixed.pem")
        self.assertEqual(main.parse_bundle(mixed, "cc.test", "mixed.pem").trust_domain, "cc.test")
        merged = main.bundle_set([self.cc.bundle(), self.cc.bundle(), self.other.bundle()])
        self.assertEqual({td: len(b.authorities) for td, b in merged.items()}, {"cc.test": 1, "other.test": 1})


class VerifyTests(Pki):
    def test_an_svid_verifies_against_the_bundle_of_its_own_trust_domain(self):
        verdict = main.verify_svid(self.web.chain, self.bundles, NOW)
        self.assertTrue(verdict.ok, verdict.detail)
        self.assertEqual((verdict.spiffe_id, verdict.trust_domain, len(verdict.chain)), ("spiffe://cc.test/web", "cc.test", 2))
        allowed = main.authorize(verdict, main.make_authorizer(["spiffe://cc.test/web"]))
        self.assertTrue(allowed.ok)
        denied = main.authorize(verdict, main.make_authorizer(["spiffe://cc.test/api"], ["other.test"]))
        self.assertEqual(denied.reason, "not-allowed")
        self.assertTrue(main.authorize(verdict, main.make_authorizer([], ["cc.test"])).ok)
        with self.assertRaisesRegex(main.InputError, "who is allowed"):
            main.make_authorizer()

    def test_each_failure_has_its_own_reason(self):
        impostor = main.issue_svid(self.other, WEB, NOW, any_trust_domain=True)
        cases = [
            ("expired", self.web.chain, self.bundles, NOW + dt.timedelta(seconds=301)),
            ("not-yet-valid", self.web.chain, self.bundles, NOW - dt.timedelta(seconds=11)),
            ("no-bundle", self.web.chain, {"other.test": self.other.bundle()}, NOW),
            ("unknown-authority", impostor.chain, self.bundles, NOW),
            ("not-a-leaf", [self.cc.cert], self.bundles, NOW),
        ]
        for reason, chain, bundles, at in cases:
            with self.subTest(reason):
                verdict = main.verify_svid(chain, bundles, at)
                self.assertFalse(verdict.ok)
                self.assertEqual(verdict.reason, reason, verdict.detail)
        verdict = main.verify_svid(impostor.chain, self.bundles, NOW)
        self.assertIn("signed by an authority of other.test, which cannot vouch for cc.test", verdict.detail)
        self.assertTrue(main.verify_svid(self.web.chain, self.bundles, NOW + dt.timedelta(seconds=300)).ok)

    def test_intermediates_presented_with_the_leaf_are_walked_to_the_bundle(self):
        key = main.generate_key()
        name = main.der_name([("2.5.4.10", "miniid"), ("2.5.4.3", "cc.test intermediate")])
        inter = main.sign_certificate(name, self.cc.cert.subject_der, key, self.cc.key, NOW - dt.timedelta(minutes=1), NOW + dt.timedelta(hours=2), [
            main.der_extension(main.OID_BASIC_CONSTRAINTS, main.der(0x30, main.der(0x01, b"\xff")), critical=True),
            main.der_extension(main.OID_KEY_USAGE, main.der_bits([5, 6]), critical=True),
            main.der_extension(main.OID_SAN, main.der(0x30, main.der(0x86, b"spiffe://cc.test"))),
            main.der_extension(main.OID_SKI, main.der(0x04, main.key_id(key))),
            main.der_extension(main.OID_AKI, main.der(0x30, main.der(0x80, self.cc.cert.subject_key_id))),
        ])
        leaf = main.issue_svid(main.Authority(inter, key), API, NOW).leaf
        verdict = main.verify_svid([leaf, inter], self.bundles, NOW)
        self.assertTrue(verdict.ok, verdict.detail)
        self.assertEqual(len(verdict.chain), 3)
        self.assertEqual(main.verify_svid([leaf], self.bundles, NOW).reason, "unknown-authority")


class Mtls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        now = main.utcnow()
        cls.cc = main.new_authority("cc.test", now)
        cls.other = main.new_authority("other.test", now)
        bundles = main.bundle_set([cls.cc.bundle(), cls.other.bundle()])
        cls.strict_bundles = main.bundle_set([cls.cc.bundle()])

        def identity(authority, spiffe_id, at=now, ttl=3600, any_td=False, trust=bundles):
            return main.Identity(main.issue_svid(authority, spiffe_id, at, ttl, any_trust_domain=any_td), trust)

        cls.identity = staticmethod(identity)
        cls.api = identity(cls.cc, API)
        cls.api_strict = identity(cls.cc, API, trust=cls.strict_bundles)
        cls.web = identity(cls.cc, WEB)
        cls.batch = identity(cls.cc, BATCH)
        cls.expired = identity(cls.cc, WEB, now - dt.timedelta(hours=2), 60)
        cls.foreign = identity(cls.other, OTHER_WEB)
        cls.impostor = identity(cls.other, WEB, any_td=True)

    def server(self, identity, allow, **options):
        server = main.MtlsServer(lambda: identity, main.make_authorizer(allow), **options)
        server.start()
        self.addCleanup(server.close)
        return server

    def call(self, server, client, **options):
        return main.call(server.address, client, main.make_authorizer(["spiffe://cc.test/api"]), timeout=5, **options)

    def test_right_id_accepted_wrong_expired_and_foreign_rejected(self):
        strict = self.server(self.api_strict, ["spiffe://cc.test/web"])
        federated = self.server(self.api, ["spiffe://cc.test/web", "spiffe://other.test/web"])
        rows = [
            ("right id", strict, self.web, "accept", "ok", "accept", ("ok",)),
            ("wrong id", strict, self.batch, "reject", "not-allowed", "reject", ("not-allowed",)),
            ("expired", strict, self.expired, "reject", "expired", "reject", ("expired", "no-certificate")),
            ("foreign ca", strict, self.foreign, "reject", "unknown-authority", "reject", ("unknown-authority", "no-certificate")),
            ("impostor", federated, self.impostor, "reject", "unknown-authority", "reject", ("unknown-authority",)),
            ("federated", federated, self.foreign, "accept", "ok", "accept", ("ok",)),
        ]
        for label, server, client, decision, reason, server_decision, server_reasons in rows:
            with self.subTest(label):
                before = len(server.decisions)
                result = self.call(server, client)
                self.assertEqual((result.decision, result.reason), (decision, reason), result.detail)
                self.assertTrue(wait_until(lambda: len(server.decisions) > before))
                logged = server.decisions[before]
                self.assertEqual(logged.decision, server_decision)
                self.assertIn(logged.reason, server_reasons, logged.detail)
        self.assertEqual(self.call(strict, self.web).replies[0], {"me": "spiffe://cc.test/api", "you": "spiffe://cc.test/web"})

    def test_python_ssl_decodes_the_uri_san_of_the_peer(self):
        server = self.server(self.api, ["spiffe://cc.test/web"])
        context = main.tls_context(self.web, server_side=False)
        with socket.create_connection(server.address, timeout=5) as raw, context.wrap_socket(raw) as tls:
            self.assertIn(("URI", "spiffe://cc.test/api"), tls.getpeercert()["subjectAltName"])
            self.assertEqual(json.loads(tls.makefile("rb").readline())["you"], "spiffe://cc.test/web")

    def test_the_client_rejects_a_server_with_the_wrong_id(self):
        rogue = self.server(self.batch, ["spiffe://cc.test/web"])
        result = self.call(rogue, self.web)
        self.assertEqual((result.decision, result.stage, result.reason, result.server_id), ("reject", "server-identity", "not-allowed", "spiffe://cc.test/batch"))

    def test_expiry_mid_connection_is_ignored_by_tls_and_enforced_on_reconnect(self):
        server = self.server(self.api, ["spiffe://cc.test/web"])
        short = self.identity(self.cc, WEB, main.utcnow(), 2)
        holder = {}
        thread = threading.Thread(target=lambda: holder.update(result=self.call(server, short, messages=["early", "late"], interval=3.4)))
        thread.start()
        thread.join(15)
        result = holder["result"]
        self.assertEqual(result.decision, "accept", result.detail)
        self.assertEqual([reply.get("echo") for reply in result.replies[1:]], ["early", "late"])
        again = self.call(server, short)
        self.assertEqual((again.decision, again.reason), ("reject", "expired"), again.detail)

    def test_enforce_expiry_closes_the_connection_when_the_peer_certificate_lapses(self):
        offset = [0.0]
        server = self.server(self.api, ["spiffe://cc.test/web"], enforce_expiry=True, clock=lambda: time.time() + offset[0])
        context = main.tls_context(self.web, server_side=False)
        with socket.create_connection(server.address, timeout=5) as raw, context.wrap_socket(raw) as tls:
            reader = tls.makefile("rb")
            self.assertIn("me", json.loads(reader.readline()))
            main.send_json(tls, {"message": "on time"})
            self.assertEqual(json.loads(reader.readline())["echo"], "on time")
            offset[0] = 7200.0
            main.send_json(tls, {"message": "too late"})
            self.assertEqual(json.loads(reader.readline())["reason"], "expired")
        self.assertTrue(wait_until(lambda: server.decisions[-1].stage == "expiry"))

    def test_the_server_rotates_its_svid_for_new_connections(self):
        offset = [0.0]
        rotator = main.Rotator(lambda: self.identity(self.cc, API), clock=lambda: time.time() + offset[0])
        server = main.MtlsServer(rotator.current, main.make_authorizer(["spiffe://cc.test/web"]))
        server.start()
        self.addCleanup(server.close)
        context = main.tls_context(self.web, server_side=False)
        seen = []
        for shift in (0.0, 0.0, 1900.0):
            offset[0] = shift
            with socket.create_connection(server.address, timeout=5) as raw, context.wrap_socket(raw) as tls:
                seen.append(hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest())
        self.assertEqual(seen[0], seen[1])
        self.assertNotEqual(seen[1], seen[2])
        self.assertEqual(rotator.rotations, 2)

    def test_tls_failures_map_to_stable_reasons(self):
        error = ssl.SSLCertVerificationError(1, "certificate verify failed")
        error.verify_code, error.verify_message = 10, "certificate has expired"
        self.assertEqual(main.tls_failure(error)[:1] + main.tls_failure(error)[2:], ("expired", 10))
        self.assertEqual(main.tls_failure(ssl.SSLError(1, "[SSL: TLSV1_ALERT_UNKNOWN_CA] tlsv1 alert unknown ca"))[0], "unknown-authority")
        self.assertEqual(main.tls_failure(ssl.SSLError(1, "[SSL: UNEXPECTED_END_OF_EARLY_DATA] no certificate returned"))[0], "no-certificate")
        self.assertEqual(main.tls_failure(ConnectionResetError(54, "Connection reset by peer"))[0], "connection-closed")


class RotationTests(Pki):
    def test_the_rotator_fetches_again_at_half_the_lifetime(self):
        clock = [NOW.timestamp()]
        key = main.generate_key()
        rotator = main.Rotator(lambda: main.Identity(main.issue_svid(self.cc, WEB, main.from_epoch(clock[0]), 100, key=key), self.bundles), clock=lambda: clock[0])
        first = rotator.current()
        self.assertEqual(main.rotation_time(first.svid.leaf), NOW + dt.timedelta(seconds=45))
        clock[0] = NOW.timestamp() + 44
        self.assertIs(rotator.current(), first)
        clock[0] = NOW.timestamp() + 45
        second = rotator.current()
        self.assertIsNot(second, first)
        self.assertEqual(second.svid.leaf.not_after - first.svid.leaf.not_after, dt.timedelta(seconds=45))
        self.assertEqual(rotator.rotations, 2)


class FakeProc(main.ProcFS):
    def __init__(self, root, starttimes):
        super().__init__(root)
        self.starttimes = list(starttimes)

    def starttime(self, pid):
        return self.starttimes.pop(0)


class AttestationTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        folder = os.path.join(self.root, "4242")
        os.makedirs(folder)
        with open(os.path.join(folder, "status"), "w") as handle:
            handle.write("Name:\tworker\nUid:\t1000\t1001\t1001\t1001\nGid:\t100\t101\t101\t101\nGroups:\t27 999\n")
        with open(os.path.join(folder, "stat"), "w") as handle:
            handle.write("4242 (my (odd) worker) S 1 4242 4242 0 -1 4194560 100 0 0 0 1 2 0 0 20 0 1 0 987654 1234 99\n")
        os.symlink("/usr/local/bin/worker", os.path.join(folder, "exe"))
        self.users = {1001: "app"}.get
        self.groups = {101: "app", 27: "sudo"}.get

    def test_selectors_come_from_the_effective_ids_groups_and_executable(self):
        proc = main.ProcFS(self.root)
        self.assertEqual(proc.starttime(4242), 987654)
        creds = main.Credentials(4242, 1001, 101)
        self.assertEqual(main.unix_selectors(creds, proc, users=self.users, groups=self.groups), [
            "unix:uid:1001", "unix:user:app", "unix:gid:101", "unix:group:app",
            "unix:supplementary_gid:27", "unix:supplementary_group:sudo", "unix:supplementary_gid:999",
        ])
        with_path = main.unix_selectors(creds, proc, discover_path=True, users=self.users, groups=self.groups)
        self.assertEqual(with_path[-1], "unix:path:/usr/local/bin/worker")

    def test_a_pid_that_changes_during_attestation_gets_nothing(self):
        creds = main.Credentials(4242, os.getuid(), 101)
        self.assertIn("unix:uid:1001", main.attest_unix(creds, FakeProc(self.root, [5, 5]), users=self.users, groups=self.groups))
        with self.assertRaisesRegex(main.AttestationError, "replaced"):
            main.attest_unix(creds, FakeProc(self.root, [5, 6]), users=self.users, groups=self.groups)
        with self.assertRaisesRegex(main.AttestationError, "replaced"):
            main.attest_unix(main.Credentials(4242, os.getuid() + 1, 101), FakeProc(self.root, [5, 5]), users=self.users, groups=self.groups)
        with self.assertRaisesRegex(main.AttestationError, "start time"):
            main.attest_unix(main.Credentials(9999, 0, 0), main.ProcFS(self.root))

    def test_peer_credentials_need_linux(self):
        left, right = socket.socketpair(socket.AF_UNIX)
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        if sys.platform.startswith("linux"):
            self.assertEqual(main.peer_credentials(left), main.Credentials(os.getpid(), os.getuid(), os.getgid()))
            selectors = main.attest_unix(main.peer_credentials(left), main.ProcFS())
            self.assertIn(f"unix:uid:{os.geteuid()}", selectors)
        else:
            with self.assertRaisesRegex(main.AttestationError, "Linux"):
                main.peer_credentials(left)


class WorkloadApiTests(Pki):
    def test_an_entry_matches_when_its_selectors_are_a_subset_of_the_discovered_ones(self):
        entries = main.parse_entries([
            {"spiffe_id": "spiffe://cc.test/web", "selectors": ["unix:uid:1001"]},
            {"spiffe_id": "spiffe://cc.test/web-admin", "selectors": ["unix:uid:1001", "unix:group:admin"], "ttl": 60},
            {"spiffe_id": "spiffe://cc.test/batch", "selectors": ["unix:uid:0"]},
        ])
        names = lambda selectors: [str(e.spiffe_id) for e in main.matching_entries(entries, selectors)]
        self.assertEqual(names(["unix:uid:1001", "unix:gid:1001"]), ["spiffe://cc.test/web"])
        self.assertEqual(names(["unix:uid:1001", "unix:group:admin"]), ["spiffe://cc.test/web", "spiffe://cc.test/web-admin"])
        self.assertEqual(names(["unix:uid:1002"]), [])
        with self.assertRaisesRegex(main.InputError, "type:key:value"):
            main.parse_entries([{"spiffe_id": "spiffe://cc.test/x", "selectors": ["uid1000"]}])

    def test_fetch_over_a_unix_socket_returns_verified_svids_and_bundles(self):
        selectors = [["unix:uid:1001"]]
        entries = main.parse_entries([
            {"spiffe_id": "spiffe://cc.test/web", "selectors": ["unix:uid:1001"], "ttl": 300},
            {"spiffe_id": "spiffe://cc.test/batch", "selectors": ["unix:uid:0"], "ttl": 300},
        ])
        lines = []
        api = main.WorkloadApi(self.cc, entries, lambda conn: selectors[0], clock=NOW.timestamp, federated=[self.other.bundle()], log=lines.append)
        path = os.path.join(tempfile.mkdtemp(), "agent.sock")
        api.bind(path)
        thread = threading.Thread(target=api.serve, args=(path,), daemon=True)
        thread.start()
        self.addCleanup(api.close)
        identity = main.fetch_identities(path)[0]
        self.assertEqual(identity.spiffe_id, "spiffe://cc.test/web")
        self.assertEqual(sorted(identity.bundles), ["cc.test", "other.test"])
        self.assertEqual(identity.svid.leaf.lifetime, 310)
        self.assertTrue(main.verify_svid(identity.svid.chain, identity.bundles, NOW).ok)
        selectors[0] = ["unix:uid:0", "unix:user:root"]
        self.assertEqual(main.fetch_identities(path)[0].spiffe_id, "spiffe://cc.test/batch")
        selectors[0] = ["unix:uid:4321"]
        with self.assertRaisesRegex(main.WorkloadApiError, "no identity issued"):
            main.fetch_identities(path)
        with socket.socket(socket.AF_UNIX) as raw:
            raw.connect(path)
            raw.sendall(b'{"method": "FetchX509SVID"}\n')
            self.assertEqual(main.read_json_line(raw)["error"], "security header missing from request")
        self.assertTrue(wait_until(lambda: len(lines) == 4))
        self.assertEqual(json.loads(lines[0])["issued"], ["spiffe://cc.test/web"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)

    def path(self, *parts):
        return os.path.join(self.folder, *parts)

    def test_id_command_reports_valid_and_invalid_ids(self):
        code, out, _ = run_cli("id", "spiffe://cc.test/ns/shop/sa/web", "--json")
        self.assertEqual((code, json.loads(out)["segments"]), (0, ["ns", "shop", "sa", "web"]))
        code, out, _ = run_cli("id", "spiffe://cc.test/web/")
        self.assertEqual(code, 1)
        self.assertIn("invalid", out)

    def test_ca_issue_inspect_and_verify_through_files(self):
        self.assertEqual(run_cli("ca", "--trust-domain", "cc.test", "--out", self.path("ca"))[0], 0)
        self.assertEqual(oct(os.stat(self.path("ca", "ca-key.pem")).st_mode & 0o777), "0o600")
        self.assertEqual(run_cli("issue", "--ca", self.path("ca"), "--id", "spiffe://cc.test/web", "--ttl", "120", "--out", self.path("web"))[0], 0)
        code, out, _ = run_cli("inspect", self.path("web", "svid.pem"), "--json")
        summary = json.loads(out)["certificates"][0]
        self.assertEqual((code, summary["spiffe_id"], summary["lifetime_seconds"], summary["problems"]), (0, "spiffe://cc.test/web", 130, []))
        code, out, _ = run_cli("inspect", self.path("ca", "bundle.json"), "--json")
        self.assertEqual((code, json.loads(out)["certificates"][0]["role"]), (0, "ca"))
        bundle = "cc.test=" + self.path("ca", "bundle.json")
        self.assertEqual(run_cli("verify", self.path("web", "svid.pem"), "--bundle", bundle, "--allow", "spiffe://cc.test/web")[0], 0)
        code, out, _ = run_cli("verify", self.path("web", "svid.pem"), "--bundle", bundle, "--allow", "spiffe://cc.test/api", "--json")
        self.assertEqual((code, json.loads(out)["reason"]), (1, "not-allowed"))
        later = (main.utcnow() + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        code, out, _ = run_cli("verify", self.path("web", "svid.pem"), "--bundle", self.path("ca", "bundle.pem"), "--at", later)
        self.assertEqual(code, 1)
        self.assertIn("expired", out)

    def test_bad_input_exits_2_with_the_reason(self):
        run_cli("ca", "--trust-domain", "cc.test", "--out", self.path("ca"))
        main.write_file(self.path("junk.pem"), "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")
        cases = [
            (["inspect", self.path("missing.pem")], "cannot read"),
            (["inspect", self.path("junk.pem")], "Certificate: expected"),
            (["issue", "--ca", self.path("ca"), "--id", "spiffe://other.test/web", "--out", self.path("x")], "not in cc.test"),
            (["issue", "--ca", self.path("ca"), "--id", "spiffe://cc.test/web", "--out", self.path("x"), "--at", "yesterday"], "--at"),
            (["serve", "--svid", self.path("ca", "ca.pem"), "--key", self.path("ca", "ca-key.pem")], "who is allowed"),
        ]
        for argv, words in cases:
            with self.subTest(argv[0]):
                code, _, err = run_cli(*argv)
                self.assertEqual(code, 2, err)
                self.assertIn(words, err)


if __name__ == "__main__":
    unittest.main()
