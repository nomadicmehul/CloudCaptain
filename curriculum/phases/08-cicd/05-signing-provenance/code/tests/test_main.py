import base64
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SIGSTORE = FIXTURES / "sigstore"
OPENSSL = FIXTURES / "openssl"
RFC6979_KEY = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
CT_LEAVES = [bytes.fromhex(h) for h in ("", "00", "10", "2021", "3031", "40414243", "5051525354555657", "606162636465666768696a6b6c6d6e6f")]
CT_ROOTS = [
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
    "fac54203e7cc696cf0dfcb42c92a1d9dbaf70ad9e621f4bd8d98662f00e3c125",
    "aeb6bcfe274b70a14fb067a5e5578264db0fa9b51af5e0ba159158f329e06e77",
    "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7",
    "4e3bbb1f7b478dcfe71fb631631519a3bca12c9aefca1612bfce4c13a86264d4",
    "76e67dadbcdf1e10e1b74ddc608abd2f98dfb16fbce75277b5232a127f2087ef",
    "ddb89be403809e325750d3d263cd78929c2942b7942a34b77e122c9594a74c8c",
    "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328",
]
BUILDER = "https://ci.example/builders/release@v1"
SOURCE = "git+https://git.example/acme/app@refs/heads/main"


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def load(name):
    return json.loads((SIGSTORE / name).read_text())


def statement_for(artifact, builder=BUILDER):
    return main.provenance_statement(
        [("app.tar.gz", hashlib.sha256(artifact).digest())],
        builder,
        external_parameters={"workflow": "release.yml"},
        resolved_dependencies=[{"uri": SOURCE, "digest": {"gitCommit": "a" * 40}}],
    )


class CurveTests(unittest.TestCase):
    def test_generator_is_on_the_curve_and_has_order_n(self):
        self.assertTrue(main.on_curve(main.G))
        minus_g = main.scalar_mult(main.N - 1, main.G)
        self.assertEqual(minus_g, (main.G[0], main.P - main.G[1]))
        self.assertIsNone(main.point_add(minus_g, main.G))
        self.assertIsNone(main.scalar_mult(0, main.G))
        self.assertFalse(main.on_curve((main.G[0], main.G[1] + 1)))

    def test_scalar_multiplication_distributes_over_addition(self):
        a, b = 0x1234567890ABCDEF, 0xFEDCBA0987654321FFFF
        left = main.scalar_mult(a + b, main.G)
        right = main.point_add(main.scalar_mult(a, main.G), main.scalar_mult(b, main.G))
        self.assertEqual(left, right)
        self.assertTrue(main.on_curve(left))
        self.assertEqual(main.point_add(main.G, main.G), main.scalar_mult(2, main.G))


class Rfc6979Tests(unittest.TestCase):
    def setUp(self):
        self.key = main.private_key_from_int(RFC6979_KEY)

    def test_public_key_matches_rfc_6979_appendix_a_2_5(self):
        self.assertEqual(self.key.public.x, 0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6)
        self.assertEqual(self.key.public.y, 0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299)

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
            self.assertEqual(next(main.rfc6979_nonces(self.key.d, digest)), k)
            self.assertEqual(main.sign_digest(self.key, digest), (r, s))
            self.assertTrue(main.verify_digest(self.key.public, digest, r, s))
        self.assertEqual(main.sign(self.key, b"sample"), main.der_signature(vectors[0][2], vectors[0][3]))

    def test_tampering_with_message_signature_or_key_fails(self):
        signature = main.sign(self.key, b"release v1.4.2")
        self.assertEqual(signature, main.sign(self.key, b"release v1.4.2"))
        self.assertTrue(main.verify(self.key.public, b"release v1.4.2", signature))
        self.assertFalse(main.verify(self.key.public, b"release v1.4.3", signature))
        self.assertFalse(main.verify(self.key.public, b"release v1.4.2", signature[:-1] + bytes([signature[-1] ^ 1])))
        self.assertFalse(main.verify(main.generate_key().public, b"release v1.4.2", signature))
        self.assertFalse(main.verify(self.key.public, b"release v1.4.2", b"not DER"))

    def test_s_and_n_minus_s_both_verify_so_signature_bytes_are_not_an_identity(self):
        digest = hashlib.sha256(b"sample").digest()
        r, s = main.sign_digest(self.key, digest)
        self.assertTrue(main.verify_digest(self.key.public, digest, r, main.N - s))
        self.assertNotEqual(main.der_signature(r, s), main.der_signature(r, main.N - s))
        self.assertFalse(main.verify_digest(self.key.public, digest, r, 0))
        self.assertFalse(main.verify_digest(self.key.public, digest, main.N, s))


class DerAndKeyTests(unittest.TestCase):
    def test_integers_get_a_leading_zero_only_when_the_high_bit_is_set(self):
        self.assertEqual(main.der_integer(0).hex(), "020100")
        self.assertEqual(main.der_integer(0x7F).hex(), "02017f")
        self.assertEqual(main.der_integer(0x80).hex(), "02020080")
        self.assertEqual(main.der_length(200).hex(), "81c8")
        self.assertEqual(main.oid_der(main.OID_EC_PUBLIC_KEY).hex(), "06072a8648ce3d0201")
        for oid in (main.OID_P256, main.OID_FULCIO_ISSUER_V2, "2.5.29.17"):
            self.assertEqual(main.oid_text(main.oid_der(oid)[2:]), oid)

    def test_signature_parsing_is_strict(self):
        good = main.der_signature(5, 0x80)
        self.assertEqual(main.parse_der_signature(good), (5, 0x80))
        for bad in (good + b"\x00", bytes.fromhex("3006020200050201ff"), bytes.fromhex("30060201800201ff"), bytes.fromhex("3003020105")):
            with self.assertRaises(main.InputError):
                main.parse_der_signature(bad)

    def test_spki_has_the_fixed_p256_prefix_and_pkcs8_round_trips(self):
        key = main.generate_key()
        self.assertTrue(key.public.der().hex().startswith("3059301306072a8648ce3d020106082a8648ce3d030107034200"))
        self.assertEqual(len(key.public.der()), 91)
        self.assertEqual(main.load_public_key(key.public.pem()), key.public)
        pkcs8 = main.pem_encode("PRIVATE KEY", main.pkcs8_der(key))
        self.assertEqual(main.load_private_key(pkcs8), key)
        self.assertEqual(main.load_private_key(main.pem_encode("EC PRIVATE KEY", main.sec1_der(key))), key)
        self.assertEqual(main.load_public_key(pkcs8), key.public)

    def test_openssl_keys_compressed_or_not_decode_to_the_same_point(self):
        full = main.load_public_key((OPENSSL / "p256.pub.pem").read_text())
        compressed = main.load_public_key((OPENSSL / "p256-compressed.pub.pem").read_text())
        self.assertEqual(full, compressed)
        self.assertTrue(main.on_curve(full.point))

    def test_openssl_signature_fixture_verifies_and_a_flipped_byte_does_not(self):
        public = main.load_public_key((OPENSSL / "p256.pub.pem").read_text())
        message = (OPENSSL / "message.txt").read_bytes()
        signature = (OPENSSL / "message.sig.der").read_bytes()
        self.assertTrue(main.verify(public, message, signature))
        self.assertFalse(main.verify(public, message + b"!", signature))
        self.assertEqual(main.read_signature(base64.b64encode(signature) + b"\n"), signature)

    def test_other_algorithms_and_cosign_keys_are_refused_by_name(self):
        for name, word in (("ed25519.pub.pem", "Ed25519"), ("p384.pub.pem", "P-384")):
            with self.assertRaises(main.InputError) as caught:
                main.load_public_key((OPENSSL / name).read_text())
            self.assertIn(word, str(caught.exception))
        with self.assertRaises(main.InputError) as caught:
            main.load_private_key("-----BEGIN ENCRYPTED SIGSTORE PRIVATE KEY-----\nAAAA\n-----END ENCRYPTED SIGSTORE PRIVATE KEY-----\n")
        self.assertIn("cosign", str(caught.exception))
        with self.assertRaises(main.InputError):
            main.decode_point(b"\x04" + b"\x01" * 64)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.key = main.generate_key()
        self.artifact = b"app-1.4.2.tar.gz contents\n"

    def test_message_bundle_has_the_sigstore_v03_shape_and_verifies(self):
        signature = main.sign(self.key, self.artifact)
        data = main.message_bundle(self.key.public, signature, hashlib.sha256(self.artifact).digest())
        self.assertEqual(data["mediaType"], "application/vnd.dev.sigstore.bundle.v0.3+json")
        self.assertEqual(base64.b64decode(data["verificationMaterial"]["publicKey"]["hint"]), hashlib.sha256(self.key.public.der()).digest())
        self.assertEqual(data["messageSignature"]["messageDigest"]["algorithm"], "SHA2_256")
        report = main.verify_blob(self.artifact, main.parse_bundle(data), [self.key.public], main.VerifyOptions())
        self.assertTrue(report.ok, main.render_report(report))
        self.assertEqual([c.name for c in report.checks], ["signature", "message-digest", "tlog"])
        self.assertIsNone(report.checks[-1].ok)

    def test_tampered_artifact_and_wrong_key_name_the_failing_check(self):
        bundle = main.parse_bundle(main.message_bundle(self.key.public, main.sign(self.key, self.artifact), hashlib.sha256(self.artifact).digest()))
        tampered = main.verify_blob(self.artifact + b"x", bundle, [self.key.public], main.VerifyOptions())
        self.assertEqual([c.name for c in tampered.failures], ["signature", "message-digest"])
        wrong_key = main.verify_blob(self.artifact, bundle, [main.generate_key().public], main.VerifyOptions())
        self.assertEqual([c.name for c in wrong_key.failures], ["signature"])
        self.assertIn("Verification FAILED", main.render_report(wrong_key))
        with self.assertRaises(main.InputError):
            main.verify_blob(self.artifact, bundle, [], main.VerifyOptions())
        with self.assertRaises(main.InputError):
            main.parse_bundle({"mediaType": "application/example"})


class DsseProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.key = main.generate_key()
        self.artifact = b"app-1.4.2.tar.gz contents\n"
        self.digest = hashlib.sha256(self.artifact).digest()

    def attest(self, statement, key=None):
        envelope = main.sign_envelope(key or self.key, main.canonical_json(statement))
        return main.parse_bundle(main.dsse_bundle((key or self.key).public, envelope))

    def test_pae_matches_the_dsse_specification_example(self):
        self.assertEqual(main.pae("http://example.com/HelloWorld", b"hello world"), b"DSSEv1 29 http://example.com/HelloWorld 11 hello world")

    def test_the_signature_covers_payload_type_and_payload(self):
        envelope = main.sign_envelope(self.key, b'{"_type":"x"}')
        self.assertEqual(main.envelope_signer(envelope, [self.key.public]), self.key.public)
        self.assertIsNone(main.envelope_signer(dict(envelope, payloadType="text/plain"), [self.key.public]))
        self.assertIsNone(main.envelope_signer(dict(envelope, payload=base64.b64encode(b'{"_type":"y"}').decode()), [self.key.public]))

    def test_statement_carries_subject_digest_builder_and_source(self):
        statement = statement_for(self.artifact)
        self.assertEqual(statement["_type"], "https://in-toto.io/Statement/v1")
        self.assertEqual(statement["predicateType"], "https://slsa.dev/provenance/v1")
        self.assertEqual(statement["subject"], [{"name": "app.tar.gz", "digest": {"sha256": self.digest.hex()}}])
        self.assertEqual(main.provenance_facts(statement), {"builder": BUILDER, "buildType": main.BUILD_TYPE, "sources": [SOURCE]})

    def test_policy_passes_for_the_expected_signer_builder_and_source(self):
        policy = main.Policy(builder_id=BUILDER, build_type=main.BUILD_TYPE, source="git+https://git.example/acme/app", signers=[self.key.public.fingerprint])
        report, statement = main.verify_attestation(self.attest(statement_for(self.artifact)), [self.key.public], self.digest, policy, main.VerifyOptions())
        self.assertTrue(report.ok, main.render_report(report))
        self.assertEqual(statement["subject"][0]["name"], "app.tar.gz")
        self.assertEqual([c.name for c in report.checks if c.ok], ["envelope-signature", "signer", "payload-type", "statement", "predicate-type", "subject", "builder", "build-type", "source"])

    def test_each_policy_violation_names_its_field(self):
        bundle = self.attest(statement_for(self.artifact, builder="https://laptop.example/me"))
        cases = [
            (main.Policy(builder_id=BUILDER), self.digest, ["builder"]),
            (main.Policy(builder_id="https://laptop.example/me", source="git+https://git.example/acme/other"), self.digest, ["source"]),
            (main.Policy(builder_id="https://laptop.example/me"), hashlib.sha256(b"other").digest(), ["subject"]),
            (main.Policy(builder_id="https://laptop.example/me", predicate_type=main.SLSA_V02), self.digest, ["predicate-type"]),
        ]
        for policy, digest, failing in cases:
            report, _ = main.verify_attestation(bundle, [self.key.public], digest, policy, main.VerifyOptions())
            self.assertEqual([c.name for c in report.failures], failing, main.render_report(report))

    def test_an_unverified_envelope_is_never_read(self):
        report, statement = main.verify_attestation(self.attest(statement_for(self.artifact)), [main.generate_key().public], self.digest, main.Policy(builder_id=BUILDER), main.VerifyOptions())
        self.assertIsNone(statement)
        self.assertEqual([c.name for c in report.checks], ["envelope-signature"])
        self.assertFalse(report.ok)


class RealSigstoreDataTests(unittest.TestCase):
    def setUp(self):
        self.staging = main.load_public_key((SIGSTORE / "rekor-staging.pub").read_text())
        self.public_good = main.load_public_key((SIGSTORE / "rekor-public-good.pub").read_text())

    def staging_options(self, bundle, **changes):
        cert = bundle.certificate
        options = dict(identity=cert.subject_alt_names[0], issuer=cert.issuer, ignore_chain=True, log_key=self.staging)
        options.update(changes)
        return main.VerifyOptions(**options)

    def test_a_staging_rekor_bundle_verifies_offline_inclusion_proof_and_all(self):
        bundle = main.parse_bundle(load("bundle_v3.txt.sigstore.json"))
        self.assertEqual(bundle.certificate.subject_alt_names, ["william@yossarian.net"])
        self.assertEqual(bundle.certificate.issuer, "https://github.com/login/oauth")
        report = main.verify_blob((SIGSTORE / "bundle_v3.txt").read_bytes(), bundle, [], self.staging_options(bundle))
        self.assertTrue(report.ok, main.render_report(report))
        passed = [c.name for c in report.checks if c.ok]
        for name in ("signature", "tlog-promise", "tlog-inclusion", "tlog-checkpoint", "tlog-entry-matches"):
            self.assertIn(name, passed)
        strict = main.verify_blob((SIGSTORE / "bundle_v3.txt").read_bytes(), bundle, [], self.staging_options(bundle, ignore_chain=False))
        self.assertEqual([c.name for c in strict.failures], ["certificate-chain"])
        wrong = main.verify_blob((SIGSTORE / "bundle_v3.txt").read_bytes(), bundle, [], self.staging_options(bundle, identity="someone@example.com"))
        self.assertEqual([c.name for c in wrong.failures], ["identity"])

    def test_one_changed_proof_hash_or_body_byte_breaks_the_real_proof(self):
        bundle = main.parse_bundle(load("bundle_v3.txt.sigstore.json"))
        entry = json.loads(json.dumps(bundle.tlog_entries[0]))
        entry["inclusionProof"]["hashes"][3] = base64.b64encode(b"\x00" * 32).decode()
        failed = [c.name for c in main.verify_tlog_entry(entry, self.staging, bundle) if not c.ok]
        self.assertEqual(failed, ["tlog-inclusion"])
        entry = json.loads(json.dumps(bundle.tlog_entries[0]))
        body = base64.b64decode(entry["canonicalizedBody"]).replace(b"5e6ae9de", b"5e6ae9df")
        entry["canonicalizedBody"] = base64.b64encode(body).decode()
        failed = [c.name for c in main.verify_tlog_entry(entry, self.staging, bundle) if not c.ok]
        self.assertEqual(failed, ["tlog-promise", "tlog-inclusion", "tlog-entry-matches"])
        failed = [c.name for c in main.verify_tlog_entry(bundle.tlog_entries[0], self.public_good, bundle) if not c.ok]
        self.assertEqual(failed, ["tlog-log-id", "tlog-promise", "tlog-checkpoint"])

    def test_npm_publish_attestation_verifies_with_npms_key_and_carries_only_a_promise(self):
        bundle = main.parse_bundle(load("npm-publish.sigstore.json"))
        npm = main.load_public_key((SIGSTORE / "npm-publish-key.pub").read_text())
        self.assertEqual(main.envelope_signer(bundle.envelope, [npm]), npm)
        self.assertIsNone(main.envelope_signer(bundle.envelope, [self.public_good]))
        results = {c.name: c.ok for c in main.verify_tlog_entry(bundle.tlog_entries[0], self.public_good)}
        self.assertEqual(results, {"tlog-log-id": True, "tlog-promise": True, "tlog-inclusion": False})

    def test_keyless_github_provenance_names_the_workflow_and_builder(self):
        bundle = main.parse_bundle(load("npm-provenance.sigstore.json"))
        self.assertEqual(bundle.certificate.subject_alt_names, ["https://github.com/sigstore/sigstore-js/.github/workflows/release.yml@refs/heads/main"])
        self.assertEqual(bundle.certificate.issuer, "https://token.actions.githubusercontent.com")
        self.assertEqual(main.envelope_signer(bundle.envelope, [bundle.certificate.public_key]), bundle.certificate.public_key)
        facts = main.provenance_facts(json.loads(base64.b64decode(bundle.envelope["payload"])))
        self.assertEqual(facts["builder"], "https://github.com/actions/runner")
        self.assertTrue(main.source_matches("git+https://github.com/sigstore/sigstore-js", facts["sources"]))
        self.assertFalse(main.source_matches("git+https://github.com/sigstore/sigstore", facts["sources"]))


class MerkleTests(unittest.TestCase):
    def leaves(self, items):
        return [main.leaf_hash(item) for item in items]

    def test_roots_match_the_certificate_transparency_reference_vectors(self):
        for size, want in enumerate(CT_ROOTS):
            self.assertEqual(main.merkle_root(self.leaves(CT_LEAVES[:size])).hex(), want, f"size {size}")

    def test_every_inclusion_proof_verifies_and_only_for_its_own_leaf(self):
        for size in range(1, 9):
            leaves = self.leaves(CT_LEAVES[:size])
            root = main.merkle_root(leaves)
            for index in range(size):
                proof = main.inclusion_path(leaves, index)
                self.assertTrue(main.verify_inclusion(leaves[index], index, size, proof, root), (index, size))
                self.assertFalse(main.verify_inclusion(main.leaf_hash(b"forged"), index, size, proof, root))
                self.assertFalse(main.verify_inclusion(leaves[index], size, size, proof, root))
                if size > 1:
                    self.assertFalse(main.verify_inclusion(leaves[index], (index + 1) % size, size, proof, root))
        self.assertEqual(len(main.inclusion_path(self.leaves(CT_LEAVES), 0)), 3)
        self.assertEqual(len(main.inclusion_path(self.leaves(CT_LEAVES[:7]), 6)), 2)

    def test_every_consistency_proof_verifies_and_a_rewritten_prefix_does_not(self):
        leaves = self.leaves(CT_LEAVES)
        for new in range(1, 9):
            for old in range(1, new + 1):
                proof = main.consistency_path(leaves[:new], old)
                old_root, new_root = main.merkle_root(leaves[:old]), main.merkle_root(leaves[:new])
                self.assertTrue(main.verify_consistency(old, new, proof, old_root, new_root), (old, new))
                if old < new:
                    forked = list(leaves[:new])
                    forked[old - 1] = main.leaf_hash(b"rewritten")
                    fork_proof = main.consistency_path(forked, old)
                    self.assertFalse(main.verify_consistency(old, new, fork_proof, old_root, main.merkle_root(forked)), (old, new))
        self.assertEqual(len(main.consistency_path(leaves, 4)), 1)
        self.assertTrue(main.verify_consistency(0, 8, [], main.merkle_root([]), main.merkle_root(leaves)))
        self.assertFalse(main.verify_consistency(5, 4, [], b"", b""))


class CheckpointAndLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_key = main.generate_key()
        self.signer = main.generate_key()
        self.log = main.TransparencyLog(Path(self.tmp.name) / "tlog")
        self.log.create(self.log_key, "minisign.test")

    def add(self, artifact):
        signature = main.sign(self.signer, artifact)
        bundle = main.parse_bundle(main.message_bundle(self.signer.public, signature, hashlib.sha256(artifact).digest()))
        index = self.log.append(self.log_key, main.hashedrekord_body(bundle.digest, signature, self.signer.public), 1700000000 + len(artifact))
        return index, bundle

    def test_checkpoint_is_a_signed_note_with_a_four_byte_key_hint(self):
        text = main.sign_checkpoint(self.log_key, "minisign.test", 5, b"\x11" * 32)
        lines = text.split("\n")
        self.assertEqual(lines[:4], ["minisign.test", "5", base64.b64encode(b"\x11" * 32).decode(), ""])
        self.assertTrue(lines[4].startswith("\u2014 minisign.test "))
        checkpoint = main.parse_checkpoint(text)
        self.assertEqual(checkpoint.signatures[0][1], self.log_key.public.log_id[:4])
        self.assertTrue(main.verify_checkpoint(checkpoint, self.log_key.public))
        self.assertFalse(main.verify_checkpoint(main.parse_checkpoint(text.replace("\n5\n", "\n6\n", 1)), self.log_key.public))
        self.assertFalse(main.verify_checkpoint(checkpoint, self.signer.public))

    def test_entries_are_proven_against_signed_checkpoints_and_bound_to_their_bundle(self):
        bundles = [self.add(artifact)[1] for artifact in (b"one", b"two", b"three")]
        for index, bundle in enumerate(bundles):
            checks = main.verify_tlog_entry(self.log.prove(index), self.log_key.public, bundle)
            self.assertTrue(all(c.ok for c in checks), [(c.name, c.detail) for c in checks])
        mismatch = {c.name: c.ok for c in main.verify_tlog_entry(self.log.prove(0), self.log_key.public, bundles[1])}
        self.assertFalse(mismatch["tlog-entry-matches"])
        older = main.verify_tlog_entry(self.log.prove(0, size=2), self.log_key.public, bundles[0])
        self.assertTrue(all(c.ok for c in older))
        self.assertTrue(main.verify_consistency_proof(self.log.consistency(1), self.log_key.public).ok)
        self.assertTrue(main.audit_log(self.log, self.log_key.public).ok)

    def test_tree_size_alone_does_not_pin_a_proof_the_signed_checkpoint_does(self):
        leaves = [main.leaf_hash(item) for item in (b"one", b"two", b"three")]
        proof = main.inclusion_path(leaves, 0)
        self.assertTrue(main.verify_inclusion(leaves[0], 0, 4, proof, main.merkle_root(leaves)))
        for artifact in (b"one", b"two", b"three"):
            self.add(artifact)
        entry = self.log.prove(0)
        entry["inclusionProof"]["treeSize"] = "4"
        results = {c.name: c.ok for c in main.verify_tlog_entry(entry, self.log_key.public)}
        self.assertTrue(results["tlog-inclusion"])
        self.assertFalse(results["tlog-checkpoint"])

    def test_a_tampered_leaf_fails_the_checkpoint_the_old_proof_and_the_audit(self):
        bundles = [self.add(artifact)[1] for artifact in (b"one", b"two", b"three")]
        before = self.log.prove(1)
        self.log.entry_path(1).write_bytes(self.log.entry_path(1).read_bytes().replace(b'"sha256"', b'"sha512"'))
        after = {c.name: c.ok for c in main.verify_tlog_entry(self.log.prove(1), self.log_key.public, bundles[1])}
        self.assertTrue(after["tlog-inclusion"])
        self.assertFalse(after["tlog-checkpoint"])
        before["canonicalizedBody"] = base64.b64encode(self.log.entry_path(1).read_bytes()).decode()
        stale = {c.name: c.ok for c in main.verify_tlog_entry(before, self.log_key.public)}
        self.assertFalse(stale["tlog-inclusion"])
        audit = main.audit_log(self.log, self.log_key.public)
        self.assertEqual([c.name for c in audit.failures], ["checkpoint-2", "checkpoint-3"])

    def test_a_rewritten_history_fails_against_the_checkpoint_you_remembered(self):
        for artifact in (b"one", b"two"):
            self.add(artifact)
        remembered = self.log.checkpoint(2)
        forked = main.TransparencyLog(Path(self.tmp.name) / "forked")
        forked.create(self.log_key, "minisign.test")
        for body in (b'{"kind":"rewritten"}', self.log.entry_path(1).read_bytes(), b'{"kind":"new"}'):
            forked.append(self.log_key, body, 1700000000)
        proof = forked.consistency(2)
        self.assertTrue(main.verify_consistency_proof(proof, self.log_key.public).ok)
        report = main.verify_consistency_proof(proof, self.log_key.public, remembered)
        self.assertEqual([c.name for c in report.failures], ["consistency"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.artifact = self.dir / "app.tar.gz"
        self.artifact.write_bytes(b"app-1.4.2 contents\n")

    def path(self, name):
        return str(self.dir / name)

    def test_keygen_sign_and_verify_with_exit_codes(self):
        code, out, _ = run(["keygen", "--output-key-prefix", self.path("signer"), "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["fingerprint"].startswith("sha256:"))
        self.assertEqual(stat.S_IMODE(os.stat(self.path("signer.key")).st_mode), 0o600)
        self.assertEqual(run(["keygen", "--output-key-prefix", self.path("signer")])[0], 2)
        code, out, _ = run(["sign-blob", "--key", self.path("signer.key"), "--bundle", self.path("app.sigstore.json"), "--output-signature", self.path("app.sig"), self.artifact])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), Path(self.path("app.sig")).read_text().strip())
        self.assertEqual(run(["verify-blob", "--key", self.path("signer.pub"), "--bundle", self.path("app.sigstore.json"), self.artifact])[0], 0)
        code, out, _ = run(["verify-blob", "--key", self.path("signer.pub"), "--signature", self.path("app.sig"), self.artifact, "--json"])
        self.assertEqual((code, json.loads(out)["ok"]), (0, True))
        self.artifact.write_bytes(b"app-1.4.2 contents, patched\n")
        code, out, _ = run(["verify-blob", "--key", self.path("signer.pub"), "--bundle", self.path("app.sigstore.json"), self.artifact])
        self.assertEqual(code, 1)
        self.assertIn("FAIL  signature", out)
        code, _, err = run(["verify-blob", "--key", self.path("missing.pub"), "--bundle", self.path("app.sigstore.json"), self.artifact])
        self.assertEqual(code, 2)
        self.assertIn("missing.pub", err)

    def test_provenance_attest_and_policy_check(self):
        run(["keygen", "--output-key-prefix", self.path("ci")])
        code, _, _ = run(["provenance", "--subject", self.artifact, "--builder-id", BUILDER, "--source", SOURCE, "--commit", "b" * 40, "--param", "workflow=release.yml", "--invocation-id", "run-42", "--output", self.path("statement.json")])
        self.assertEqual(code, 0)
        statement = json.loads(Path(self.path("statement.json")).read_text())
        self.assertEqual(statement["predicate"]["runDetails"]["metadata"], {"invocationId": "run-42"})
        self.assertEqual(run(["attest", "--key", self.path("ci.key"), "--statement", self.path("statement.json"), "--bundle", self.path("att.sigstore.json")])[0], 0)
        envelope = json.loads(Path(self.path("att.sigstore.json")).read_text())["dsseEnvelope"]
        self.assertEqual(base64.b64decode(envelope["payload"]), Path(self.path("statement.json")).read_bytes())
        Path(self.path("policy.json")).write_text(json.dumps({"builderId": BUILDER, "source": "git+https://git.example/acme/app"}))
        verify = ["verify-attestation", "--key", self.path("ci.pub"), "--bundle", self.path("att.sigstore.json"), "--artifact", self.artifact]
        self.assertEqual(run(verify + ["--policy", self.path("policy.json")])[0], 0)
        code, out, _ = run(verify + ["--policy", self.path("policy.json"), "--builder-id", "https://other.example/builder"])
        self.assertEqual(code, 1)
        self.assertIn("FAIL  builder", out)
        code, _, err = run(verify)
        self.assertEqual(code, 2)
        self.assertIn("builderId is required", err)
        Path(self.path("bad-policy.json")).write_text(json.dumps({"builderID": BUILDER}))
        self.assertEqual(run(verify + ["--policy", self.path("bad-policy.json")])[0], 2)

    def test_log_commands_from_init_to_audit(self):
        for name in ("log", "signer"):
            run(["keygen", "--output-key-prefix", self.path(name)])
        run(["sign-blob", "--key", self.path("signer.key"), "--bundle", self.path("app.sigstore.json"), self.artifact])
        self.assertEqual(run(["log", "init", "--log", self.path("tlog"), "--key", self.path("log.key"), "--origin", "cc.test"])[0], 0)
        code, out, _ = run(["log", "add", "--log", self.path("tlog"), "--key", self.path("log.key"), "--bundle", self.path("app.sigstore.json"), "--public-key", self.path("signer.pub"), "--output", self.path("entry.json")])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("logIndex 0\ncc.test\n1\n"))
        self.assertEqual(run(["log", "add", "--log", self.path("tlog"), "--key", self.path("log.key"), "--bundle", self.path("app.sigstore.json"), "--public-key", self.path("log.pub")])[0], 2)
        run(["log", "add", "--log", self.path("tlog"), "--key", self.path("log.key"), "--bundle", self.path("app.sigstore.json"), "--public-key", self.path("signer.pub")])
        self.assertEqual(run(["log", "verify", "--log-key", self.path("log.pub"), "--entry", self.path("entry.json"), "--bundle", self.path("app.sigstore.json")])[0], 0)
        verify = ["verify-blob", "--key", self.path("signer.pub"), "--bundle", self.path("app.sigstore.json"), "--log-key", self.path("log.pub"), self.artifact]
        self.assertEqual(run(verify)[0], 1)
        self.assertEqual(run(verify + ["--tlog-entry", self.path("entry.json")])[0], 0)
        self.assertEqual(run(["log", "consistency", "--log", self.path("tlog"), "--old", "1", "--output", self.path("consistency.json")])[0], 0)
        self.assertEqual(run(["log", "verify-consistency", "--log-key", self.path("log.pub"), "--proof", self.path("consistency.json")])[0], 0)
        self.assertEqual(run(["log", "audit", "--log", self.path("tlog"), "--log-key", self.path("log.pub")])[0], 0)
        entry = Path(self.path("tlog")) / "entries" / "00000000.json"
        entry.write_bytes(entry.read_bytes().replace(b"hashedrekord", b"hashedrekorD"))
        code, out, _ = run(["log", "audit", "--log", self.path("tlog"), "--log-key", self.path("log.pub")])
        self.assertEqual(code, 1)
        self.assertIn("FAIL  checkpoint-1", out)

    def test_inspect_explains_a_real_bundle_and_a_checkpoint(self):
        code, out, _ = run(["inspect", SIGSTORE / "bundle_v3.txt.sigstore.json", "--json"])
        summary = json.loads(out)
        self.assertEqual((code, summary["content"], summary["tlogEntries"]), (0, "messageSignature", 1))
        self.assertEqual(summary["certificate"]["subjectAltNames"], ["william@yossarian.net"])
        Path(self.path("cp.txt")).write_text(load("bundle_v3.txt.sigstore.json")["verificationMaterial"]["tlogEntries"][0]["inclusionProof"]["checkpoint"]["envelope"])
        code, out, _ = run(["inspect", self.path("cp.txt"), "--json"])
        self.assertEqual(json.loads(out)["treeSize"], 25901138)
        code, out, _ = run(["inspect", SIGSTORE / "npm-provenance.sigstore.json", "--json"])
        self.assertEqual(json.loads(out)["facts"]["buildType"], "https://github.com/npm/cli/gha/v2")


if __name__ == "__main__":
    unittest.main()
