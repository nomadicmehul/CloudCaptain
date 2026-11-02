import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
NOW = 1793534400
ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/cc-deploy"
GH = "token.actions.githubusercontent.com"
KEYS = {}


def key(name="a"):
    if name not in KEYS:
        KEYS[name] = main.generate_key(1024, f"test-{name}")
    return KEYS[name]


def fixture(*parts):
    with open(os.path.join(FIXTURES, *parts), encoding="utf-8") as handle:
        return json.load(handle)


def jobs():
    return main.load_jobs(fixture("jobs.json"))


def provider(name="github"):
    return main.load_provider(fixture("providers", name + ".json"), ACCOUNT)


def policy(name):
    return fixture("trust-policies", name + ".json")


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class CountingFetch:
    def __init__(self, *issuers):
        self.inner = main.issuer_fetch(*issuers)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return self.inner(url)


def lab(keys=("a",), providers=("github",), clock=None):
    clock = clock or Clock()
    issuer = main.Issuer(main.GITHUB_ISSUER, [key(k) for k in keys], clock=clock)
    fetch = CountingFetch(issuer)
    verifier = main.Verifier([provider(p) for p in providers], fetch=fetch, clock=clock)
    return issuer, fetch, verifier, clock


def token_for(issuer, job="deploy-prod", audience=main.AWS_AUDIENCE, now=NOW):
    return issuer.mint(jobs()[job], audience, now)


def forged(header, claims, signing_key=None):
    body = main.b64url_encode(main.compact_json(header)) + "." + main.b64url_encode(main.compact_json(claims))
    signature = main.rsa_sign(signing_key, body.encode()) if signing_key else b""
    return body + "." + main.b64url_encode(signature)


def run_cli(*argv, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    try:
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(list(argv))
    finally:
        sys.stdin = old_stdin
    return code, out.getvalue(), err.getvalue()


class EncodingTests(unittest.TestCase):
    def test_base64url_has_no_padding_and_rejects_the_standard_alphabet(self):
        self.assertEqual(main.b64url_encode(b"\xfb\xff"), "-_8")
        self.assertEqual(main.b64url_decode("-_8"), b"\xfb\xff")
        self.assertEqual(main.b64url_decode(""), b"")
        for bad in ("+/8=", "ab=c", "abcde", 42):
            with self.assertRaises(ValueError):
                main.b64url_decode(bad)

    def test_rfc7638_thumbprint_is_the_kid(self):
        doc = fixture("rfc7638-thumbprint.json")
        self.assertEqual(main.jwk_thumbprint(doc["jwk"]), doc["thumbprint"])
        self.assertEqual(key("a").kid, "test-a")
        self.assertEqual(main.key_from_jwk(key("a").public_jwk()).kid, "test-a")
        self.assertEqual(main.key_from_jwk({k: v for k, v in doc["jwk"].items() if k != "kid"}).kid, doc["thumbprint"])


class PrimeTests(unittest.TestCase):
    def test_miller_rabin_rejects_carmichael_numbers_and_accepts_mersenne_primes(self):
        for composite in (561, 41041, 825265, 321197185, 5394826801, 2**64 + 1):
            self.assertFalse(main.is_probable_prime(composite), composite)
        for prime in (2, 3, 1999, 2**61 - 1, 2**127 - 1, 2**521 - 1):
            self.assertTrue(main.is_probable_prime(prime), prime)
        self.assertFalse(main.is_probable_prime(1))

    def test_fixed_bases_can_be_fooled_and_random_bases_are_not(self):
        psi = 3825123056546413051
        self.assertTrue(main.is_probable_prime(psi, bases=[2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31]))
        self.assertFalse(main.is_probable_prime(psi, bases=[37]))
        self.assertFalse(main.is_probable_prime(psi))

    def test_generated_key_satisfies_the_rsa_identities(self):
        k = key("a")
        self.assertEqual(k.bits, 1024)
        self.assertEqual(k.p * k.q, k.n)
        self.assertEqual(k.e, 65537)
        self.assertEqual((k.e * k.d) % ((k.p - 1) * (k.q - 1) // __import__("math").gcd(k.p - 1, k.q - 1)), 1)
        self.assertEqual((k.dp, k.dq, (k.qi * k.q) % k.p), (k.d % (k.p - 1), k.d % (k.q - 1), 1))
        message = 123456789
        self.assertEqual(pow(main.rsa_private_op(k, message), k.e, k.n), message)
        with self.assertRaises(main.InputError):
            main.generate_key(512)


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.vector = fixture("rfc7515-a2.json")
        self.rfc_key = main.key_from_jwk({"kty": "RSA", "n": self.vector["n"], "e": self.vector["e"]})
        header, payload, signature = self.vector["jws"].split(".")
        self.signing_input = f"{header}.{payload}".encode()
        self.signature = main.b64url_decode(signature)

    def test_rfc7515_appendix_a2_signature_verifies(self):
        self.assertEqual(self.rfc_key.bits, 2048)
        self.assertTrue(main.rsa_verify(self.rfc_key, self.signing_input, self.signature))
        self.assertFalse(main.rsa_verify(self.rfc_key, self.signing_input + b"x", self.signature))
        self.assertFalse(main.rsa_verify(self.rfc_key, self.signing_input, self.signature[1:]))
        self.assertFalse(main.rsa_verify(self.rfc_key, self.signing_input, main.int_bytes(self.rfc_key.n, 256)))
        claims = main.decode_jwt(self.vector["jws"]).claims
        self.assertEqual((claims["iss"], claims["exp"]), ("joe", 1300819380))

    def test_the_rfc_signature_opens_to_the_pkcs1_v15_encoding(self):
        opened = main.int_bytes(pow(main.bytes_int(self.signature), self.rfc_key.e, self.rfc_key.n), 256)
        self.assertEqual(opened[:2], b"\x00\x01")
        self.assertEqual(opened[2:204], b"\xff" * 202)
        self.assertEqual(opened[-52:-32], b"\x00" + main.SHA256_DIGEST_INFO)
        self.assertEqual(opened, main.emsa_pkcs1_v15(self.signing_input, 256))

    def test_signatures_are_deterministic_and_bound_to_key_and_message(self):
        message = b"header.payload"
        first, second = main.rsa_sign(key("a"), message), main.rsa_sign(key("a"), message)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 128)
        self.assertTrue(main.rsa_verify(key("a").public(), message, first))
        self.assertFalse(main.rsa_verify(key("b"), message, first))
        flipped = bytes([first[0] ^ 1]) + first[1:]
        self.assertFalse(main.rsa_verify(key("a"), message, flipped))
        with self.assertRaises(main.InputError):
            main.rsa_sign(key("a").public(), message)

    def test_a_crt_fault_is_caught_before_the_signature_leaves(self):
        k = key("a")
        broken = main.RsaKey(k.n, k.e, k.kid, k.d, k.p, k.q, k.dp + 2, k.dq, k.qi)
        with self.assertRaises(RuntimeError):
            main.rsa_sign(broken, b"payload")


class KeyFormatTests(unittest.TestCase):
    def test_spki_pem_round_trips_with_the_rsa_algorithm_identifier(self):
        k = key("a")
        der = main.spki_der(k)
        self.assertEqual(der[:2], b"\x30\x81")
        self.assertIn(bytes.fromhex("300d06092a864886f70d0101010500"), der)
        pem = main.to_pem("PUBLIC KEY", der)
        self.assertTrue(pem.startswith("-----BEGIN PUBLIC KEY-----\n"))
        self.assertTrue(all(len(line) <= 64 for line in pem.splitlines()))
        back = main.public_key_from_pem(pem, "back")
        self.assertEqual((back.n, back.e, back.kid), (k.n, k.e, "back"))
        rsa_only = main.to_pem("RSA PUBLIC KEY", main.der(0x30, main.der_integer(k.n) + main.der_integer(k.e)))
        self.assertEqual(main.public_key_from_pem(rsa_only).n, k.n)
        with self.assertRaises(main.InputError):
            main.public_key_from_pem("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")

    def test_der_integers_are_minimal_twos_complement(self):
        self.assertEqual(main.der_integer(65537), bytes.fromhex("0203010001"))
        self.assertEqual(main.der_integer(0x80), bytes.fromhex("02020080"))
        self.assertEqual(main.der_integer(0x7F), bytes.fromhex("02017f"))
        self.assertEqual(main.der(0x04, b"x" * 200)[:3], bytes.fromhex("0481c8"))

    def test_githubs_published_jwks_and_discovery_parse(self):
        keys = main.parse_jwks(fixture("github-jwks.json"))
        self.assertGreaterEqual(len(keys), 2)
        self.assertTrue(all(k.bits == 2048 and k.e == 65537 for k in keys.values()))
        discovery = fixture("github-openid-configuration.json")
        self.assertEqual(discovery["issuer"], main.GITHUB_ISSUER)
        self.assertEqual(discovery["id_token_signing_alg_values_supported"], ["RS256"])
        self.assertTrue(set(main.CLAIMS_SUPPORTED) <= set(discovery["claims_supported"]))

    def test_private_jwk_round_trips_and_the_public_one_leaks_nothing(self):
        k = key("a")
        back = main.key_from_jwk(k.private_jwk())
        self.assertEqual((back.n, back.d, back.qi, back.kid), (k.n, k.d, k.qi, k.kid))
        self.assertFalse(set(k.public_jwk()) & set(main.PRIVATE_JWK_MEMBERS))


class ClaimsTests(unittest.TestCase):
    def claims(self, name, audience=main.AWS_AUDIENCE):
        return main.github_claims(jobs()[name], audience, main.GITHUB_ISSUER, NOW)

    def test_subject_follows_githubs_precedence_environment_then_pull_request_then_ref(self):
        self.assertEqual(self.claims("deploy-prod")["sub"], "repo:octo-org/octo-repo:environment:prod")
        self.assertEqual(self.claims("pull-request")["sub"], "repo:octo-org/octo-repo:pull_request")
        self.assertEqual(self.claims("feature-push")["sub"], "repo:octo-org/octo-repo:ref:refs/heads/feature/login")
        tag = self.claims("release-tag")
        self.assertEqual((tag["sub"], tag["ref_type"]), ("repo:octo-org/octo-repo:ref:refs/tags/v1.4.0", "tag"))
        self.assertNotIn("environment", self.claims("main-push"))

    def test_colons_are_escaped_and_immutable_subjects_carry_ids(self):
        self.assertEqual(self.claims("colon-environment")["sub"], "repo:octo-org/octo-repo:environment:Production%3AV1")
        self.assertEqual(self.claims("immutable-main")["sub"], "repo:octo-org@123456/octo-repo@456789:ref:refs/heads/main")

    def test_customized_subject_template_and_reusable_workflow(self):
        claims = self.claims("reusable-deploy")
        self.assertEqual(claims["sub"], "repo:octo-org/octo-repo:environment:prod:job_workflow_ref:octo-org/octo-automation/.github/workflows/oidc.yml@refs/heads/main")
        owner_only = main.load_job({"repository": "monalisa/app", "repository_visibility": "private", "include_claim_keys": ["repository_owner", "repository_visibility"]})
        self.assertEqual(main.github_claims(owner_only, "", main.GITHUB_ISSUER, NOW)["sub"], "repository_owner:monalisa:repository_visibility:private")
        with self.assertRaises(main.InputError):
            main.github_claims(main.load_job({"repository": "a/b", "include_claim_keys": ["nope"]}), "", main.GITHUB_ISSUER, NOW)

    def test_default_audience_is_the_owner_url_and_times_follow_githubs_example(self):
        claims = self.claims("main-push", audience="")
        self.assertEqual(claims["aud"], "https://github.com/octo-org")
        self.assertEqual((claims["exp"] - claims["iat"], claims["iat"] - claims["nbf"]), (300, 600))
        self.assertEqual(claims["iss"], main.GITHUB_ISSUER)
        self.assertEqual(claims["job_workflow_ref"], "octo-org/octo-repo/.github/workflows/deploy.yml@refs/heads/main")
        with self.assertRaises(main.InputError):
            main.load_job({"repository": "no-slash"})


class IssuerHttpTests(unittest.TestCase):
    def setUp(self):
        self.issuer = main.Issuer(main.GITHUB_ISSUER, [key("a")])
        self.lines = []
        self.server = main.make_server(self.issuer, log=self.lines.append)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.bearer = self.issuer.register("deploy-prod", jobs()["deploy-prod"])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path, headers=None):
        request = urllib.request.Request(self.server.base_url + path, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), json.loads(exc.read())

    def test_discovery_and_jwks_are_served_with_cache_headers_and_no_private_members(self):
        status, headers, discovery = self.get("/.well-known/openid-configuration")
        self.assertEqual((status, discovery["issuer"]), (200, main.GITHUB_ISSUER))
        self.assertEqual(discovery["jwks_uri"], self.server.base_url + "/.well-known/jwks")
        self.assertIn("max-age=3600", headers["Cache-Control"])
        status, _, jwks = self.get("/.well-known/jwks")
        self.assertEqual([k["kid"] for k in jwks["keys"]], ["test-a"])
        self.assertFalse(set(jwks["keys"][0]) & set(main.PRIVATE_JWK_MEMBERS))

    def test_the_idtoken_endpoint_needs_the_runners_bearer_token(self):
        self.assertEqual(self.get("/idtoken?api-version=2.0")[0], 401)
        status, _, body = self.get("/idtoken?api-version=2.0", {"Authorization": "bearer not-issued"})
        self.assertEqual(status, 401)
        self.assertIn("id-token: write", body["message"])
        self.assertEqual(self.get("/nothing")[0], 404)

    def test_a_requested_token_carries_the_audience_and_verifies_over_http(self):
        token = main.request_id_token(self.server.token_url, self.bearer, "sts.amazonaws.com")
        claims = main.decode_jwt(token).claims
        self.assertEqual((claims["aud"], claims["sub"]), ("sts.amazonaws.com", "repo:octo-org/octo-repo:environment:prod"))
        verifier = main.Verifier([provider()], discovery_url=self.server.base_url + "/.well-known/openid-configuration")
        verified = verifier.verify(token)
        self.assertEqual(verified.kid, "test-a")
        self.assertTrue(any("GET /idtoken 200 job=deploy-prod aud=sts.amazonaws.com" in line for line in self.lines))
        azure = main.decode_jwt(main.request_id_token(self.server.token_url, self.bearer, "api://AzureADTokenExchange")).claims
        self.assertEqual(azure["aud"], "api://AzureADTokenExchange")
        with self.assertRaises(main.CommandError):
            main.request_id_token(self.server.token_url, "wrong")


class VerifierTests(unittest.TestCase):
    def test_the_jwks_is_fetched_once_and_cached_until_its_ttl(self):
        issuer, fetch, verifier, clock = lab()
        for _ in range(3):
            verifier.verify(token_for(issuer))
        self.assertEqual(len(fetch.urls), 2)
        clock.now += main.JWKS_TTL
        verifier.verify(token_for(issuer, now=clock.now))
        self.assertEqual(len(fetch.urls), 4)

    def test_rotation_refetches_once_and_garbage_kids_do_not_storm_the_issuer(self):
        issuer, fetch, verifier, clock = lab()
        verifier.verify(token_for(issuer))
        clock.now += 30
        issuer.rotate(key("b"))
        with self.assertRaises(main.StsError) as raised:
            verifier.verify(token_for(issuer, now=clock.now))
        self.assertIn("no key with kid 'test-b'", raised.exception.message)
        self.assertEqual(verifier.caches[main.GITHUB_ISSUER].fetches, 1)
        clock.now += main.JWKS_MIN_REFRESH
        self.assertEqual(verifier.verify(token_for(issuer, now=clock.now)).kid, "test-b")
        self.assertEqual(verifier.caches[main.GITHUB_ISSUER].fetches, 2)
        old = main.encode_jwt(main.github_claims(jobs()["deploy-prod"], main.AWS_AUDIENCE, main.GITHUB_ISSUER, int(clock.now)), key("a"))
        self.assertEqual(verifier.verify(old).kid, "test-a")
        issuer.retire("test-a")
        clock.now += main.JWKS_TTL
        with self.assertRaises(main.StsError):
            verifier.verify(main.encode_jwt(main.github_claims(jobs()["deploy-prod"], main.AWS_AUDIENCE, main.GITHUB_ISSUER, int(clock.now)), key("a")))

    def test_alg_none_and_hmac_confusion_are_refused_before_any_key_is_used(self):
        issuer, fetch, verifier, _ = lab()
        claims = main.decode_jwt(token_for(issuer)).claims
        for alg in ("none", "HS256", "RS512"):
            with self.assertRaises(main.StsError) as raised:
                verifier.verify(forged({"alg": alg, "kid": "test-a"}, claims))
            self.assertIn("unsupported alg", raised.exception.message)
        import hashlib
        import hmac
        body = main.b64url_encode(main.compact_json({"alg": "HS256", "kid": "test-a"})) + "." + main.b64url_encode(main.compact_json(claims))
        mac = hmac.new(main.spki_der(key("a")), body.encode(), hashlib.sha256).digest()
        with self.assertRaises(main.StsError):
            verifier.verify(body + "." + main.b64url_encode(mac))
        self.assertEqual(fetch.urls, [])

    def test_a_key_in_the_header_is_ignored_and_a_foreign_signature_fails(self):
        issuer, _, verifier, _ = lab()
        claims = main.decode_jwt(token_for(issuer)).claims
        attacker = key("mallory")
        with self.assertRaises(main.StsError) as raised:
            verifier.verify(forged({"alg": "RS256", "kid": "test-a", "jwk": attacker.public_jwk()}, claims, attacker))
        self.assertIn("signature does not verify", raised.exception.message)
        with self.assertRaises(main.StsError) as raised:
            verifier.verify(forged({"alg": "RS256", "kid": attacker.kid, "jwk": attacker.public_jwk()}, claims, attacker))
        self.assertIn("no key with kid", raised.exception.message)
        good = token_for(issuer)
        header, payload, signature = good.split(".")
        changed = dict(claims, sub="repo:mallory/anything:ref:refs/heads/main")
        with self.assertRaises(main.StsError):
            verifier.verify(header + "." + main.b64url_encode(main.compact_json(changed)) + "." + signature)
        for broken in ("a.b", "!!.??.--", main.b64url_encode(b"[]") + ".e30.AA"):
            with self.assertRaises(main.StsError) as raised:
                verifier.verify(broken)
            self.assertEqual(raised.exception.code, "InvalidIdentityToken")

    def test_the_issuer_must_have_a_provider_and_its_discovery_must_name_it(self):
        issuer, _, verifier, _ = lab()
        stranger = main.Issuer("https://ci.example.net", [key("b")])
        with self.assertRaises(main.StsError) as raised:
            verifier.verify(stranger.mint(jobs()["stranger"], main.AWS_AUDIENCE, NOW))
        self.assertIn("No OpenIDConnect provider found in your account for https://ci.example.net", raised.exception.message)
        liar = main.Issuer("https://ci.example.net", [key("a")])
        mixed = main.Verifier([provider()], fetch=lambda url: main.compact_json(liar.discovery()), clock=Clock())
        with self.assertRaises(main.StsError) as raised:
            mixed.verify(token_for(issuer))
        self.assertIn("names issuer 'https://ci.example.net'", raised.exception.message)
        offline = main.Verifier([provider()], fetch=main.issuer_fetch(), clock=Clock())
        with self.assertRaises(main.StsError) as raised:
            offline.verify(token_for(issuer))
        self.assertEqual(raised.exception.code, "IDPCommunicationError")

    def test_the_audience_must_be_a_client_id_and_azp_takes_precedence(self):
        issuer, _, verifier, _ = lab()
        with self.assertRaises(main.StsError) as raised:
            verifier.verify(token_for(issuer, audience="api://AzureADTokenExchange"))
        self.assertIn("Incorrect token audience", raised.exception.message)
        claims = main.decode_jwt(token_for(issuer)).claims
        both = dict(claims, aud="https://github.com/octo-org", azp="sts.amazonaws.com")
        self.assertEqual(verifier.verify(main.encode_jwt(both, key("a"))).audience, "sts.amazonaws.com")
        listed = dict(claims, aud=["https://example.com", "sts.amazonaws.com"])
        self.assertEqual(verifier.verify(main.encode_jwt(listed, key("a"))).audience, "https://example.com")

    def test_a_provider_without_client_ids_accepts_tokens_meant_for_another_service(self):
        issuer = main.Issuer(main.GITHUB_ISSUER, [key("a")])
        careless = main.Verifier([main.Provider(GH, [], f"arn:aws:iam::{ACCOUNT}:oidc-provider/{GH}")], fetch=main.issuer_fetch(issuer), clock=Clock())
        replayed = token_for(issuer, audience="https://vault.example.com")
        self.assertEqual(careless.verify(replayed).audience, "https://vault.example.com")
        with self.assertRaises(main.StsError):
            lab()[2].verify(replayed)

    def test_time_claims_with_leeway(self):
        issuer, _, verifier, clock = lab()
        token = token_for(issuer)
        clock.now = NOW + 300 + 30
        self.assertTrue(verifier.verify(token))
        clock.now = NOW + 300 + 60
        with self.assertRaises(main.StsError) as raised:
            verifier.verify(token)
        self.assertEqual(raised.exception.code, "ExpiredTokenException")
        self.assertIn("2026-11-01T12:05:00Z", raised.exception.message)
        clock.now = NOW
        claims = main.decode_jwt(token).claims
        for change, word in ((dict(nbf=NOW + 120), "not valid before"), (dict(iat=NOW + 120), "issued in the future"), (dict(exp=float(NOW + 300)), "integer timestamp"), (dict(exp=True), "integer timestamp")):
            with self.assertRaises(main.StsError) as raised:
                verifier.verify(main.encode_jwt(dict(claims, **change), key("a")))
            self.assertIn(word, raised.exception.message)
        without_iat = {k: v for k, v in claims.items() if k != "iat"}
        with self.assertRaises(main.StsError):
            verifier.verify(main.encode_jwt(without_iat, key("a")))

    def test_a_jwks_that_publishes_private_members_or_too_many_keys_is_refused(self):
        with self.assertRaises(main.StsError) as raised:
            main.parse_jwks({"keys": [key("a").private_jwk()]})
        self.assertIn("Couldn't retrieve verification key", raised.exception.message)
        self.assertIn("compromised", raised.exception.message)
        many = {"keys": [dict(key("a").public_jwk(), kid=str(i)) for i in range(101)]}
        with self.assertRaises(main.StsError):
            main.parse_jwks(many)
        with self.assertRaises(main.StsError):
            main.parse_jwks({"keys": []})
        mixed = main.parse_jwks({"keys": [dict(key("a").public_jwk(), use="enc"), {"kty": "EC", "kid": "ec"}, key("b").public_jwk()]})
        self.assertEqual(list(mixed), ["test-b"])


class ConditionTests(unittest.TestCase):
    def holds(self, operator, wanted, present):
        return main.key_holds(operator, wanted, present)

    def test_string_operators_and_globs(self):
        self.assertTrue(self.holds("StringEquals", ["repo:a/b:ref:refs/heads/main"], ["repo:a/b:ref:refs/heads/main"]))
        self.assertFalse(self.holds("StringEquals", ["repo:a/b:*"], ["repo:a/b:pull_request"]))
        self.assertTrue(self.holds("StringLike", ["repo:a/b:*"], ["repo:a/b:pull_request"]))
        self.assertTrue(self.holds("StringLike", ["repo:a/b:ref:refs/heads/release-?"], ["repo:a/b:ref:refs/heads/release-7"]))
        self.assertFalse(self.holds("StringLike", ["repo:a/b:ref:refs/heads/release-?"], ["repo:a/b:ref:refs/heads/release-10"]))
        self.assertFalse(self.holds("StringLike", ["repo:A/B:*"], ["repo:a/b:pull_request"]))
        self.assertTrue(self.holds("StringEqualsIgnoreCase", ["repo:A/B:pull_request"], ["repo:a/b:pull_request"]))
        self.assertTrue(self.holds("StringLike", ["repo:a/x:*", "repo:a/b:*"], ["repo:a/b:ref:refs/heads/main"]))
        self.assertTrue(self.holds("StringLike", ["a.b"], ["a.b"]))
        self.assertFalse(self.holds("StringLike", ["a.b"], ["axb"]))

    def test_missing_keys_negation_ifexists_null_and_set_operators(self):
        self.assertFalse(self.holds("StringEquals", ["x"], []))
        self.assertTrue(self.holds("StringNotEquals", ["x"], []))
        self.assertTrue(self.holds("StringEqualsIfExists", ["x"], []))
        self.assertFalse(self.holds("StringNotEquals", ["a", "b"], ["b"]))
        self.assertTrue(self.holds("StringNotEquals", ["a", "b"], ["c"]))
        self.assertTrue(self.holds("Null", ["true"], []))
        self.assertFalse(self.holds("Null", ["true"], ["x"]))
        self.assertTrue(self.holds("ForAnyValue:StringLike", ["unauthenticated"], ["authenticated", "unauthenticated"]))
        self.assertFalse(self.holds("ForAnyValue:StringLike", ["x"], []))
        self.assertTrue(self.holds("ForAllValues:StringEquals", ["a"], []))
        self.assertFalse(self.holds("ForAllValues:StringEquals", ["a"], ["a", "b"]))

    def test_unknown_operators_are_policy_errors(self):
        for operator in ("StringEqualz", "NumericEquals", "ForEveryValue:StringEquals", "NullIfExists"):
            with self.assertRaises(main.PolicyError):
                main.parse_operator(operator)

    def test_condition_keys_follow_awss_claim_mapping(self):
        claims = main.github_claims(jobs()["deploy-prod"], main.AWS_AUDIENCE, main.GITHUB_ISSUER, NOW)
        context = main.request_context(claims, provider())
        self.assertEqual(context[f"{GH}:sub"], ["repo:octo-org/octo-repo:environment:prod"])
        self.assertEqual(context[f"{GH}:aud"], ["sts.amazonaws.com"])
        self.assertIn(f"{GH}:job_workflow_ref", context)
        self.assertNotIn(f"{GH}:repository_owner", context)
        self.assertNotIn(f"{GH}:environment", context)
        hybrid = main.request_context(dict(claims, azp="client-a"), provider())
        self.assertEqual((hybrid[f"{GH}:aud"], hybrid[f"{GH}:oaud"]), (["client-a"], ["sts.amazonaws.com"]))
        gitlab = main.request_context({"sub": "project_path:g/p:ref_type:branch:ref:main", "aud": "x", "project_id": "67890", "repository_owner": "g"}, provider("gitlab"))
        self.assertEqual(gitlab["gitlab.com:project_id"], ["67890"])


class TrustTests(unittest.TestCase):
    def test_every_documented_case(self):
        rows = main.run_cases(fixture("aws-doc-cases.json"), FIXTURES, bits=1024, now=NOW)
        self.assertGreaterEqual(len(rows), 25)
        self.assertEqual([r for r in rows if not r["ok"]], [])
        self.assertEqual({r["got"] for r in rows}, {"allowed", "AccessDenied", "InvalidIdentityToken", "ExpiredTokenException"})

    def test_explicit_deny_wins_and_principal_and_action_must_match(self):
        claims = main.github_claims(jobs()["pull-request"], main.AWS_AUDIENCE, main.GITHUB_ISSUER, NOW)
        decision = main.evaluate_trust(policy("github-deny-pr"), claims, provider())
        self.assertEqual(decision.decision, "explicitDeny")
        self.assertEqual([s.applies for s in decision.statements], [True, True])
        other = main.Provider(GH, ["sts.amazonaws.com"], "arn:aws:iam::210987654321:oidc-provider/" + GH)
        self.assertEqual(main.evaluate_trust(policy("github-any-ref"), claims, other).decision, "implicitDeny")
        wrong_action = json.loads(json.dumps(policy("github-any-ref")).replace("AssumeRoleWithWebIdentity", "AssumeRole"))
        self.assertEqual(main.evaluate_trust(wrong_action, claims, provider()).decision, "implicitDeny")
        self.assertEqual(main.evaluate_trust(json.loads(json.dumps(policy("github-any-ref")).replace("sts:AssumeRoleWithWebIdentity", "sts:*")), claims, provider()).decision, "allowed")
        with self.assertRaises(main.PolicyError):
            main.evaluate_trust({"Statement": [{"Effect": "Maybe"}]}, claims, provider())

    def test_the_outcome_names_the_failing_condition(self):
        issuer, _, verifier, _ = lab()
        sts = main.Sts(verifier, [main.Role(ROLE, policy("github-environment"))], clock=Clock())
        outcome = sts.assume(token_for(issuer, job="main-push"), ROLE)
        self.assertEqual((outcome.code, outcome.error.message), ("AccessDenied", main.NOT_AUTHORIZED))
        failed = [c for c in outcome.trust.statements[0].checks if not c.passed]
        self.assertEqual([(c.key, c.actual) for c in failed], [(f"{GH}:sub", ["repo:octo-org/octo-repo:ref:refs/heads/main"])])
        text = main.render_outcome(outcome)
        self.assertIn("FAIL  StringEquals token.actions.githubusercontent.com:sub", text)
        self.assertIn("An error occurred (AccessDenied) when calling the AssumeRoleWithWebIdentity operation", text)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.issuer, _, verifier, self.clock = lab()
        self.sts = main.Sts(verifier, [main.Role(ROLE, policy("github-environment"), max_session=3600)], secret=b"k" * 32, clock=self.clock)

    def test_duration_and_session_name_are_validated(self):
        token = token_for(self.issuer)
        for duration in (899, 43201):
            self.assertEqual(self.sts.assume(token, ROLE, duration=duration).code, "ValidationError")
        too_long = self.sts.assume(token, ROLE, duration=3601)
        self.assertEqual((too_long.code, too_long.error.message), ("ValidationError", main.TOO_LONG))
        self.assertEqual(self.sts.assume(token, ROLE, session_name="has space").code, "ValidationError")
        self.assertEqual(self.sts.assume(token, ROLE + "-missing").code, "AccessDenied")
        self.assertTrue(self.sts.assume(token, ROLE, duration=900).allowed)

    def test_the_session_is_short_lived_and_names_the_role_without_its_path(self):
        pathed = "arn:aws:iam::123456789012:role/ci/deploy/cc-deploy"
        sts = main.Sts(self.sts.verifier, [main.Role(pathed, policy("github-environment"))], clock=self.clock)
        outcome = sts.assume(token_for(self.issuer), pathed, "run-42", 900)
        doc = outcome.to_json()
        self.assertEqual(doc["AssumedRoleUser"]["Arn"], "arn:aws:sts::123456789012:assumed-role/cc-deploy/run-42")
        self.assertTrue(doc["AssumedRoleUser"]["AssumedRoleId"].startswith("AROA") and doc["AssumedRoleUser"]["AssumedRoleId"].endswith(":run-42"))
        self.assertEqual(doc["Credentials"]["Expiration"], "2026-11-01T12:15:00Z")
        self.assertRegex(doc["Credentials"]["AccessKeyId"], r"^ASIA[A-Z2-7]{16}$")
        self.assertEqual((doc["SubjectFromWebIdentityToken"], doc["Audience"], doc["Provider"]), ("repo:octo-org/octo-repo:environment:prod", "sts.amazonaws.com", main.GITHUB_ISSUER))

    def test_the_session_token_is_bound_to_the_sts_secret_and_expires(self):
        session = self.sts.assume(token_for(self.issuer), ROLE, duration=900).session
        identity = main.session_identity(session.session_token, b"k" * 32, NOW + 899)
        self.assertEqual(identity["arn"], session.arn)
        with self.assertRaises(main.StsError) as raised:
            main.session_identity(session.session_token, b"k" * 32, NOW + 900)
        self.assertEqual(raised.exception.code, "ExpiredToken")
        for secret, token in ((b"x" * 32, session.session_token), (b"k" * 32, session.session_token[:-2] + "AA"), (b"k" * 32, "garbage!")):
            with self.assertRaises(main.StsError) as raised:
                main.session_identity(token, secret, NOW)
            self.assertEqual(raised.exception.code, "InvalidClientTokenId")


class AuditTests(unittest.TestCase):
    def codes(self, name, provider_name="github"):
        return {(f.severity, f.code) for f in main.audit_trust_policy(policy(name), provider(provider_name))}

    def test_each_threat_policy_is_flagged(self):
        self.assertIn(("CRITICAL", "missing-subject"), self.codes("github-no-sub"))
        self.assertTrue({("CRITICAL", "missing-subject"), ("HIGH", "negated-subject")} <= self.codes("github-not-pr"))
        self.assertTrue({("CRITICAL", "missing-subject"), ("HIGH", "unmapped-key-passes")} <= self.codes("github-unmapped-owner"))
        self.assertEqual(self.codes("github-owner-prefix"), {("HIGH", "wildcard-owner")})
        self.assertEqual(self.codes("github-any-repo"), {("MEDIUM", "wildcard-repository")})
        self.assertEqual(self.codes("github-any-ref"), {("MEDIUM", "wildcard-context")})
        message = next(f.message for f in main.audit_trust_policy(policy("github-no-sub")) if f.code == "missing-subject")
        self.assertIn("MalformedPolicyDocument", message)

    def test_more_threats_built_inline(self):
        def statement(condition, principal=None):
            return {"Statement": [{"Effect": "Allow", "Principal": principal or {"Federated": f"arn:aws:iam::{ACCOUNT}:oidc-provider/{GH}"}, "Action": "sts:AssumeRoleWithWebIdentity", "Condition": condition}]}
        aud = {f"{GH}:aud": "sts.amazonaws.com"}
        found = lambda doc, p=None: {f.code for f in main.audit_trust_policy(doc, p)}
        self.assertIn("wildcard-subject", found(statement({"StringLike": {f"{GH}:sub": "*"}, "StringEquals": aud})))
        self.assertIn("literal-wildcard", found(statement({"StringEquals": dict(aud, **{f"{GH}:sub": "repo:octo-org/octo-repo:*"})})))
        self.assertIn("pull-request-subject", found(statement({"StringEquals": dict(aud, **{f"{GH}:sub": "repo:octo-org/octo-repo:pull_request"})})))
        self.assertIn("missing-audience", found(statement({"StringEquals": {f"{GH}:sub": "repo:octo-org/octo-repo:environment:prod"}})))
        self.assertIn("public-principal", found(statement({"StringEquals": aud}, "*")))
        self.assertIn("foreign-key", found(statement({"StringEquals": dict(aud, **{f"{GH}:sub": "repo:o/r:environment:prod", "gitlab.com:project_id": "1"})})))
        self.assertIn("unmapped-key-fails", found(statement({"StringEquals": dict(aud, **{f"{GH}:sub": "repo:o/r:environment:prod", f"{GH}:repository_owner": "o"})})))
        self.assertIn("provider-any-audience", found(policy("github-environment"), main.Provider(GH, [], "arn")))
        self.assertIn("provider-many-audiences", found(policy("github-environment"), provider("github-two-audiences")))

    def test_the_documented_least_privilege_policies_are_clean(self):
        for name in ("github-branch", "github-environment", "github-immutable", "github-reusable"):
            self.assertEqual(self.codes(name), set(), name)
        self.assertEqual(self.codes("gitlab-branch", "gitlab"), {("LOW", "missing-audience")})


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key = os.path.join(self.tmp.name, "key.json")
        with open(self.key, "w") as handle:
            json.dump(key("a").private_jwk(), handle)
        self.jwks = os.path.join(self.tmp.name, "jwks.json")
        code, out, _ = run_cli("jwks", "--key", self.key)
        self.assertEqual(code, 0)
        with open(self.jwks, "w") as handle:
            handle.write(out)

    def tearDown(self):
        self.tmp.cleanup()

    def mint(self, job):
        code, out, err = run_cli("mint", "--key", self.key, "--jobs", os.path.join(FIXTURES, "jobs.json"), "--job", job, "--audience", "sts.amazonaws.com", "--now", str(NOW))
        self.assertEqual(code, 0, err)
        return out.strip()

    def assume(self, token, policy_name="github-environment", *extra):
        return run_cli("assume", "--token", token, "--role-arn", ROLE, "--trust-policy", os.path.join(FIXTURES, "trust-policies", policy_name + ".json"), "--provider", os.path.join(FIXTURES, "providers", "github.json"), "--jwks", self.jwks, "--now", str(NOW + 5), *extra)

    def test_mint_and_assume_return_awss_response_shape_and_exit_codes(self):
        code, out, _ = self.assume(self.mint("deploy-prod"), "github-environment", "--json", "--duration-seconds", "900")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(set(doc) - {"minioidc"}, {"Credentials", "SubjectFromWebIdentityToken", "AssumedRoleUser", "Provider", "Audience"})
        self.assertEqual(doc["minioidc"]["decision"], "allowed")
        code, out, _ = self.assume(self.mint("feature-push"), "github-environment", "--json")
        self.assertEqual((code, json.loads(out)["Error"]["Code"]), (1, "AccessDenied"))
        code, out, _ = self.assume(self.mint("feature-push"), "github-environment")
        self.assertIn("FAIL  StringEquals", out)
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w") as handle:
            handle.write("{not json")
        code, _, err = run_cli("assume", "--token", "x.y.z", "--role-arn", ROLE, "--trust-policy", bad, "--provider", os.path.join(FIXTURES, "providers", "github.json"))
        self.assertEqual(code, 2)
        self.assertIn("is not JSON", err)
        code, out, _ = run_cli("assume", "--token", "-", "--role-arn", ROLE, "--trust-policy", os.path.join(FIXTURES, "trust-policies", "github-environment.json"), "--provider", os.path.join(FIXTURES, "providers", "github.json"), "--jwks", self.jwks, "--json", stdin="not-a-token\n")
        self.assertEqual((code, json.loads(out)["Error"]["Code"]), (1, "InvalidIdentityToken"))

    def test_verify_and_decode(self):
        token = self.mint("deploy-prod")
        code, out, _ = run_cli("verify", token, "--provider", os.path.join(FIXTURES, "providers", "github.json"), "--jwks", self.jwks, "--now", str(NOW), "--json")
        self.assertEqual((code, json.loads(out)["claims"]["sub"]), (0, "repo:octo-org/octo-repo:environment:prod"))
        code, out, _ = run_cli("verify", token, "--provider", os.path.join(FIXTURES, "providers", "github.json"), "--jwks", self.jwks, "--now", str(NOW + 3600))
        self.assertEqual(code, 1)
        self.assertIn("ExpiredTokenException", out)
        code, out, _ = run_cli("decode", "-", stdin=token + "\n")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("UNVERIFIED"))

    def test_jwk_to_pem_and_pem_to_jwk(self):
        code, pem, _ = run_cli("jwk-to-pem", "--jwks", self.jwks, "--kid", "test-a")
        self.assertEqual(code, 0)
        path = os.path.join(self.tmp.name, "pub.pem")
        with open(path, "w") as handle:
            handle.write(pem)
        code, out, _ = run_cli("pem-to-jwk", path, "--kid", "again")
        self.assertEqual(json.loads(out)["keys"][0]["n"], key("a").public_jwk()["n"])
        self.assertEqual(run_cli("jwk-to-pem", "--jwks", self.jwks, "--kid", "nope")[0], 1)

    def test_request_without_the_runner_environment_explains_the_permission(self):
        saved = {name: os.environ.pop(name, None) for name in ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")}
        try:
            code, _, err = run_cli("request", "--audience", "sts.amazonaws.com")
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value
        self.assertEqual(code, 2)
        self.assertIn("id-token: write", err)

    def test_audit_and_keygen(self):
        code, out, _ = run_cli("audit", "--trust-policy", os.path.join(FIXTURES, "trust-policies", "github-no-sub.json"), "--json")
        self.assertEqual((code, json.loads(out)[0]["code"]), (1, "missing-subject"))
        code, out, _ = run_cli("audit", "--trust-policy", os.path.join(FIXTURES, "trust-policies", "github-any-ref.json"))
        self.assertEqual(code, 0)
        self.assertIn("wildcard-context", out)
        out_path = os.path.join(self.tmp.name, "new.json")
        code, out, _ = run_cli("keygen", "--bits", "1024", "--out", out_path)
        self.assertEqual(code, 0)
        self.assertEqual(oct(os.stat(out_path).st_mode & 0o777), "0o600")
        self.assertEqual(run_cli("keygen", "--bits", "1000", "--out", out_path)[0], 2)


if __name__ == "__main__":
    unittest.main()
