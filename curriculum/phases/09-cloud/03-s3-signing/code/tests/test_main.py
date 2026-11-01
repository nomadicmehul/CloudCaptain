import base64
import contextlib
import hashlib
import http.client
import io
import json
import os
import sys
import threading
import unittest
import urllib.parse
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
OWNER = main.Key("AKIAOWNER0000000001", "owner-secret", "arn:aws:iam::111122223333:user/alice")
ROOT = main.Key("AKIAROOT00000000001", "root-secret", "arn:aws:iam::111122223333:root")
PARTNER = main.Key("AKIAPARTNER00000001", "partner-secret", "arn:aws:iam::444455556666:user/bob")
NOW = main.parse_amz_date("20261101T120000Z")


def creds(key):
    return main.Credentials(key.access_key, key.secret_key)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class SigningTests(unittest.TestCase):
    def test_every_vector_in_the_aws_sigv4_test_suite_matches_header_and_query_signing(self):
        vectors = main.load_vectors(os.path.join(FIXTURES, "aws-sigv4-test-suite.json"))
        self.assertEqual(len(vectors), 38)
        results = [result for vector in vectors for result in main.run_vector(vector)]
        self.assertEqual({r.mode for r in results}, {"header", "query"})
        for result in results:
            with self.subTest(vector=result.name, mode=result.mode):
                self.assertTrue(result.passed, result.mismatches)

    def test_the_s3_api_reference_examples_match_including_unsigned_payload_presign(self):
        results = {(r.name, r.mode): r for v in main.load_vectors(os.path.join(FIXTURES, "s3-api-examples.json")) for r in main.run_vector(v)}
        self.assertEqual(len(results), 5)
        self.assertTrue(all(r.passed for r in results.values()), [r.__dict__ for r in results.values() if not r.passed])
        self.assertTrue(results[("s3-presigned-get", "query")].canonical_request.endswith("\nUNSIGNED-PAYLOAD"))

    def test_uri_encode_uses_percent_20_keeps_unreserved_and_encodes_slash_only_outside_paths(self):
        self.assertEqual(main.uri_encode("a b+c~d_e.f-g"), "a%20b%2Bc~d_e.f-g")
        self.assertEqual(main.uri_encode("photos/2026/ሴ.jpg", keep_slash=True), "photos/2026/%E1%88%B4.jpg")
        self.assertEqual(main.uri_encode("a/b"), "a%2Fb")
        self.assertEqual(main.uri_encode("$&=*"), "%24%26%3D%2A")

    def test_s3_signs_the_key_as_given_while_other_services_normalize_the_path(self):
        self.assertEqual(main.canonical_uri("/bucket//a/./b/../c"), "/bucket//a/./b/../c")
        self.assertEqual(main.canonical_uri("/bucket//a/./b/../c", normalize=True), "/bucket/a/c")
        self.assertEqual(main.canonical_uri(""), "/")

    def test_canonical_query_sorts_encoded_names_and_keeps_empty_values(self):
        query = [("prefix", "J"), ("max-keys", "2"), ("uploads", ""), ("Zeta", "a b")]
        self.assertEqual(main.canonical_query(query), "Zeta=a%20b&max-keys=2&prefix=J&uploads=")

    def test_canonical_headers_lowercase_trim_collapse_and_join_repeats_in_order(self):
        headers = [("Host", "example.com"), ("My-Header", "  b   c "), ("my-header", "a"), ("X-Unsigned", "x")]
        self.assertEqual(main.canonical_headers(headers, ["host", "my-header"]), "host:example.com\nmy-header:b c,a\n")

    def test_signing_key_is_scoped_so_another_day_or_region_gives_another_signature(self):
        base = main.signing_key("secret", "20261101", "us-east-1", "s3")
        self.assertEqual(len(base), 32)
        self.assertNotEqual(base, main.signing_key("secret", "20261102", "us-east-1", "s3"))
        self.assertNotEqual(base, main.signing_key("secret", "20261101", "eu-west-1", "s3"))

    def test_presign_refuses_more_than_seven_days(self):
        with self.assertRaises(ValueError):
            main.presign("GET", "/b/k", [], [("Host", "h")], creds(OWNER), "us-east-1", "s3", NOW, 604801)


class ServerCase(unittest.TestCase):
    region = "us-east-1"
    min_part_size = 1024

    def setUp(self):
        self.clock = Clock(NOW)
        self.store = main.Store(self.clock, self.min_part_size)
        keys = {key.access_key: key for key in (OWNER, ROOT, PARTNER)}
        self.service = main.S3Service(self.store, keys, self.region, self.clock)
        self.server = main.make_server(self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.endpoint = f"http://127.0.0.1:{self.port}"
        self.owner = main.S3Client(self.endpoint, creds(OWNER), clock=self.clock)
        self.partner = main.S3Client(self.endpoint, creds(PARTNER), clock=self.clock)
        self.root = main.S3Client(self.endpoint, creds(ROOT), clock=self.clock)
        self.anonymous = main.S3Client(self.endpoint, None, clock=self.clock)
        self.assertEqual(self.owner.create_bucket("shop-assets").status, 200)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def raw(self, method, target, headers=None, body=b""):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, target, body=body, headers=headers or {})
            response = connection.getresponse()
            return main.HTTPResult(response.status, dict(response.getheaders()), response.read())
        finally:
            connection.close()

    def signed_headers(self, method, path, body=b"", extra=None, secret=None):
        key = main.Credentials(OWNER.access_key, secret or OWNER.secret_key)
        pairs = [("Host", f"127.0.0.1:{self.port}")] + list((extra or {}).items())
        signed = main.sign_with_headers(method, path, [], pairs, main.sha256_hex(body), key, self.region, "s3", NOW)
        return dict(signed.headers), signed


class AuthenticationTests(ServerCase):
    def test_signed_put_get_head_delete_round_trip_with_md5_etag(self):
        put = self.owner.put_object("shop-assets", "docs/read me.txt", b"hello world\n", {"Content-Type": "text/plain", "x-amz-meta-team": "web"})
        self.assertEqual(put.status, 200)
        self.assertEqual(put.headers["ETag"], '"' + hashlib.md5(b"hello world\n").hexdigest() + '"')
        got = self.owner.get_object("shop-assets", "docs/read me.txt")
        self.assertEqual((got.status, got.body, got.headers["Content-Type"], got.headers["x-amz-meta-team"]), (200, b"hello world\n", "text/plain", "web"))
        head = self.owner.call("HEAD", "shop-assets", "docs/read me.txt")
        self.assertEqual((head.status, head.body, head.headers["Content-Length"]), (200, b"", "12"))
        self.assertEqual(self.owner.delete_object("shop-assets", "docs/read me.txt").status, 204)
        self.assertEqual(self.owner.get_object("shop-assets", "docs/read me.txt").error_code(), "NoSuchKey")

    def test_one_changed_signature_byte_is_signaturedoesnotmatch_with_the_servers_canonical_request(self):
        headers, signed = self.signed_headers("GET", "/shop-assets")
        flipped = signed.signature[:-1] + ("0" if signed.signature[-1] != "0" else "1")
        headers["Authorization"] = signed.authorization.replace(signed.signature, flipped)
        result = self.raw("GET", "/shop-assets", headers)
        self.assertEqual((result.status, result.error_code()), (403, "SignatureDoesNotMatch"))
        self.assertEqual(result.error_field("CanonicalRequest"), signed.canonical_request.replace("&", "&amp;"))
        self.assertEqual(result.error_field("SignatureProvided"), flipped)

    def test_a_wrong_secret_and_an_unknown_key_fail_differently(self):
        headers, _ = self.signed_headers("GET", "/shop-assets", secret="not-the-secret")
        self.assertEqual(self.raw("GET", "/shop-assets", headers).error_code(), "SignatureDoesNotMatch")
        stranger = main.S3Client(self.endpoint, main.Credentials("AKIANOBODY000000001", "x"), clock=self.clock)
        self.assertEqual(stranger.call("GET", "shop-assets", query=[("list-type", "2")]).error_code(), "InvalidAccessKeyId")

    def test_a_request_signed_for_another_region_names_the_expected_region(self):
        elsewhere = main.S3Client(self.endpoint, creds(OWNER), region="eu-west-1", clock=self.clock)
        result = elsewhere.call("GET", "shop-assets", query=[("list-type", "2")])
        self.assertEqual((result.status, result.error_code(), result.error_field("Region")), (400, "AuthorizationHeaderMalformed", "us-east-1"))
        self.assertIn("the region 'eu-west-1' is wrong; expecting 'us-east-1'", result.error_field("Message"))

    def test_a_clock_more_than_fifteen_minutes_off_is_requesttimetooskewed(self):
        late = main.S3Client(self.endpoint, creds(OWNER), clock=Clock(NOW - 901))
        self.assertEqual(late.call("GET", "shop-assets", query=[("list-type", "2")]).error_code(), "RequestTimeTooSkewed")
        nearly = main.S3Client(self.endpoint, creds(OWNER), clock=Clock(NOW - 899))
        self.assertEqual(nearly.call("GET", "shop-assets", query=[("list-type", "2")]).status, 200)

    def test_a_body_that_does_not_match_the_signed_sha256_is_rejected_after_the_signature_passes(self):
        headers, _ = self.signed_headers("PUT", "/shop-assets/a.txt", body=b"what was signed")
        result = self.raw("PUT", "/shop-assets/a.txt", headers, b"what was sent!!")
        self.assertEqual((result.status, result.error_code()), (400, "XAmzContentSHA256Mismatch"))

    def test_an_x_amz_header_that_is_not_signed_is_refused(self):
        headers, _ = self.signed_headers("PUT", "/shop-assets/a.txt", body=b"x")
        headers["x-amz-acl"] = "public-read"
        result = self.raw("PUT", "/shop-assets/a.txt", headers, b"x")
        self.assertEqual((result.status, result.error_code(), result.error_field("HeadersNotSigned")), (403, "AccessDenied", "x-amz-acl"))

    def test_sigv2_and_a_missing_content_sha256_header_are_refused_with_s3s_messages(self):
        result = self.raw("GET", "/shop-assets", {"Authorization": "AWS AKIAOWNER0000000001:c2lnbmF0dXJl"})
        self.assertEqual(result.error_code(), "InvalidRequest")
        self.assertIn("Please use AWS4-HMAC-SHA256", result.error_field("Message"))
        headers, _ = self.signed_headers("GET", "/shop-assets")
        headers.pop("X-Amz-Content-Sha256")
        self.assertIn("x-amz-content-sha256", self.raw("GET", "/shop-assets", headers).error_field("Message"))

    def test_aws_chunked_body_with_a_crc32_trailer_is_decoded_and_checked(self):
        data = b"hello chunked world"
        trailer = base64.b64encode(zlib.crc32(data).to_bytes(4, "big")).decode()
        body = f"{len(data):x}\r\n".encode() + data + b"\r\n0\r\nx-amz-checksum-crc32:" + trailer.encode() + b"\r\n\r\n"
        extra = {"Content-Encoding": "aws-chunked", "x-amz-decoded-content-length": str(len(data)), "x-amz-trailer": "x-amz-checksum-crc32"}
        pairs = [("Host", f"127.0.0.1:{self.port}")] + list(extra.items())
        signed = main.sign_with_headers("PUT", "/shop-assets/chunked.txt", [], pairs, main.STREAMING_UNSIGNED_TRAILER, creds(OWNER), self.region, "s3", NOW)
        self.assertEqual(self.raw("PUT", "/shop-assets/chunked.txt", dict(signed.headers), body).status, 200)
        self.assertEqual(self.owner.get_object("shop-assets", "chunked.txt").body, data)
        broken = body.replace(trailer.encode(), b"AAAAAA==")
        self.assertEqual(self.raw("PUT", "/shop-assets/chunked.txt", dict(signed.headers), broken).error_code(), "BadDigest")


class PresignTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.owner.put_object("shop-assets", "report.csv", b"a,b\n1,2\n")

    def target(self, url):
        parts = urllib.parse.urlsplit(url)
        return parts.path + "?" + parts.query

    def test_a_presigned_get_works_without_any_header_until_it_expires(self):
        url = self.owner.presign("GET", "shop-assets", "report.csv", expires=300)
        self.assertEqual(self.raw("GET", self.target(url)).body, b"a,b\n1,2\n")
        self.clock.now = NOW + 301
        expired = self.raw("GET", self.target(url))
        self.assertEqual((expired.status, expired.error_code(), expired.error_field("Message")), (403, "AccessDenied", "Request has expired"))

    def test_changing_one_byte_of_the_signature_or_the_key_breaks_a_presigned_url(self):
        url = self.owner.presign("GET", "shop-assets", "report.csv")
        tampered = url[:-1] + ("0" if url[-1] != "0" else "1")
        self.assertEqual(self.raw("GET", self.target(tampered)).error_code(), "SignatureDoesNotMatch")
        self.owner.put_object("shop-assets", "secret.csv", b"no")
        other_key = url.replace("/report.csv?", "/secret.csv?")
        self.assertEqual(self.raw("GET", self.target(other_key)).error_code(), "SignatureDoesNotMatch")

    def test_a_presigned_put_uploads_and_its_method_is_part_of_the_signature(self):
        url = self.owner.presign("PUT", "shop-assets", "upload.bin")
        self.assertEqual(self.raw("PUT", self.target(url), body=b"\x00\x01").status, 200)
        self.assertEqual(self.raw("GET", self.target(url)).error_code(), "SignatureDoesNotMatch")

    def test_x_amz_expires_over_a_week_is_rejected_by_the_server(self):
        signed = main.presign("GET", "/shop-assets/report.csv", [], [("Host", f"127.0.0.1:{self.port}")], creds(OWNER), self.region, "s3", NOW, 3600)
        query = [(k, "604801" if k == "X-Amz-Expires" else v) for k, v in signed.query]
        target = "/shop-assets/report.csv?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
        result = self.raw("GET", target)
        self.assertEqual((result.status, result.error_code()), (400, "AuthorizationQueryParametersError"))

    def test_inspect_reports_expiry_and_verifies_with_the_secret(self):
        url = self.owner.presign("GET", "shop-assets", "report.csv", expires=600)
        report = main.inspect_presigned(url, NOW + 60, OWNER.secret_key)
        self.assertEqual((report.access_key, report.region, report.expires, int(report.seconds_left), report.verified), (OWNER.access_key, "us-east-1", 600, 540, True))
        self.assertFalse(main.inspect_presigned(url, NOW, "wrong").verified)


class ListingTests(ServerCase):
    def setUp(self):
        super().setUp()
        for key in ["photos/2026/a.jpg", "photos/2026/b.jpg", "photos/2025/c.jpg", "photos-old.txt", "docs/readme.txt", "top.txt"]:
            self.owner.put_object("shop-assets", key, b"x")

    def test_delimiter_rolls_keys_up_into_common_prefixes(self):
        root = self.owner.list_objects_v2("shop-assets", delimiter="/")
        self.assertEqual((root["keys"], root["prefixes"]), (["photos-old.txt", "top.txt"], ["docs/", "photos/"]))
        photos = self.owner.list_objects_v2("shop-assets", prefix="photos/", delimiter="/")
        self.assertEqual((photos["keys"], photos["prefixes"]), ([], ["photos/2025/", "photos/2026/"]))
        flat = self.owner.list_objects_v2("shop-assets", prefix="photos/")
        self.assertEqual(flat["keys"], ["photos/2025/c.jpg", "photos/2026/a.jpg", "photos/2026/b.jpg"])

    def test_pages_count_prefixes_and_keys_and_resume_after_a_whole_prefix(self):
        seen, token, pages = [], "", 0
        while True:
            page = self.owner.list_objects_v2("shop-assets", delimiter="/", max_keys=2, token=token)
            self.assertLessEqual(page["key_count"], 2)
            seen += page["keys"] + page["prefixes"]
            pages += 1
            if not page["truncated"]:
                break
            token = page["next"]
        self.assertEqual((pages, sorted(seen)), (2, ["docs/", "photos-old.txt", "photos/", "top.txt"]))

    def test_encoding_type_url_encodes_keys_in_the_response(self):
        self.owner.put_object("shop-assets", "space key+plus.txt", b"x")
        result = self.owner.call("GET", "shop-assets", query=[("list-type", "2"), ("prefix", "space"), ("encoding-type", "url")])
        self.assertIn(b"<Key>space%20key%2Bplus.txt</Key>", result.body)
        self.assertIn(b"<EncodingType>url</EncodingType>", result.body)


class ObjectFeatureTests(ServerCase):
    def test_multipart_etag_is_the_md5_of_the_part_md5s_with_the_part_count(self):
        parts = [b"a" * 1024, b"b" * 1024, b"c" * 10]
        result = self.owner.multipart_upload("shop-assets", "big.bin", parts)
        self.assertEqual(result.status, 200, result.body)
        expected = hashlib.md5(b"".join(hashlib.md5(p).digest() for p in parts)).hexdigest() + "-3"
        self.assertIn(f"<ETag>&quot;{expected}&quot;</ETag>".encode(), result.body)
        got = self.owner.get_object("shop-assets", "big.bin")
        self.assertEqual((got.body, got.headers["ETag"]), (b"".join(parts), f'"{expected}"'))
        self.assertNotEqual(got.headers["ETag"], main.md5_etag(got.body))

    def test_a_part_below_the_minimum_size_fails_at_complete_with_entitytoosmall(self):
        result = self.owner.multipart_upload("shop-assets", "small.bin", [b"a" * 10, b"b" * 10])
        self.assertEqual((result.status, result.error_code()), (400, "EntityTooSmall"))

    def test_versioning_keeps_every_put_and_delete_writes_a_marker(self):
        self.assertEqual(self.owner.put_versioning("shop-assets", "Enabled").status, 200)
        first = self.owner.put_object("shop-assets", "config.json", b"v1").headers["x-amz-version-id"]
        second = self.owner.put_object("shop-assets", "config.json", b"v2").headers["x-amz-version-id"]
        self.assertNotEqual(first, second)
        deleted = self.owner.delete_object("shop-assets", "config.json")
        self.assertEqual(deleted.headers.get("x-amz-delete-marker"), "true")
        gone = self.owner.get_object("shop-assets", "config.json")
        self.assertEqual((gone.status, gone.headers.get("x-amz-delete-marker")), (404, "true"))
        self.assertEqual(self.owner.get_object("shop-assets", "config.json", version_id=first).body, b"v1")
        listing = self.owner.call("GET", "shop-assets", query=[("versions", "")]).body
        self.assertEqual((listing.count(b"<Version>"), listing.count(b"<DeleteMarker>")), (2, 1))
        self.owner.delete_object("shop-assets", "config.json", version_id=deleted.headers["x-amz-version-id"])
        self.assertEqual(self.owner.get_object("shop-assets", "config.json").body, b"v2")

    def test_suspended_versioning_overwrites_the_null_version_only(self):
        self.owner.put_versioning("shop-assets", "Enabled")
        kept = self.owner.put_object("shop-assets", "k", b"enabled").headers["x-amz-version-id"]
        self.owner.put_versioning("shop-assets", "Suspended")
        self.assertEqual(self.owner.put_object("shop-assets", "k", b"null-1").headers["x-amz-version-id"], "null")
        self.owner.put_object("shop-assets", "k", b"null-2")
        versions = [v.version_id for v in self.store.buckets["shop-assets"].objects["k"]]
        self.assertEqual(versions, [kept, "null"])
        self.assertEqual(self.owner.get_object("shop-assets", "k").body, b"null-2")

    def test_range_get_returns_206_with_content_range(self):
        self.owner.put_object("shop-assets", "r.txt", b"0123456789")
        result = self.owner.get_object("shop-assets", "r.txt", byte_range="bytes=2-5")
        self.assertEqual((result.status, result.body, result.headers["Content-Range"]), (206, b"2345", "bytes 2-5/10"))
        self.assertEqual(self.owner.get_object("shop-assets", "r.txt", byte_range="bytes=-3").body, b"789")
        self.assertEqual(self.owner.get_object("shop-assets", "r.txt", byte_range="bytes=20-").status, 416)


class BucketPolicyTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.owner.put_object("shop-assets", "public/logo.png", b"png")
        self.owner.put_object("shop-assets", "private/keys.txt", b"secret")

    def policy(self, *statements):
        result = self.owner.put_policy("shop-assets", {"Version": "2012-10-17", "Statement": list(statements)})
        self.assertEqual(result.status, 204, result.body)

    def test_same_account_is_allowed_other_accounts_and_anonymous_are_not(self):
        self.assertEqual(self.owner.get_object("shop-assets", "public/logo.png").status, 200)
        denied = self.partner.get_object("shop-assets", "public/logo.png")
        self.assertEqual((denied.error_code(), denied.error_field("Message")), ("AccessDenied", "Access Denied"))
        self.assertEqual(self.anonymous.get_object("shop-assets", "public/logo.png").error_field("Message"), "Access Denied")

    def test_a_public_read_statement_on_a_prefix_opens_only_that_prefix(self):
        self.policy({"Sid": "PublicLogo", "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::shop-assets/public/*"})
        self.assertEqual(self.anonymous.get_object("shop-assets", "public/logo.png").status, 200)
        self.assertEqual(self.anonymous.get_object("shop-assets", "private/keys.txt").status, 403)
        self.assertEqual(self.partner.get_object("shop-assets", "public/logo.png").status, 200)

    def test_an_explicit_deny_beats_the_owners_own_access(self):
        self.policy({"Sid": "NoPrivate", "Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": "arn:aws:s3:::shop-assets/private/*"})
        denied = self.owner.get_object("shop-assets", "private/keys.txt")
        self.assertEqual(denied.error_code(), "AccessDenied")
        self.assertIn("explicit deny in a resource-based policy (statement NoPrivate)", denied.error_field("Message"))
        self.assertEqual(self.owner.get_object("shop-assets", "public/logo.png").status, 200)

    def test_cross_account_access_needs_a_statement_naming_the_principal_or_its_account(self):
        self.policy({"Effect": "Allow", "Principal": {"AWS": "444455556666"}, "Action": ["s3:GetObject", "s3:ListBucket"], "Resource": ["arn:aws:s3:::shop-assets", "arn:aws:s3:::shop-assets/*"]})
        self.assertEqual(self.partner.get_object("shop-assets", "private/keys.txt").status, 200)
        self.assertEqual(self.partner.put_object("shop-assets", "x", b"x").error_code(), "AccessDenied")

    def test_a_condition_on_s3_authtype_blocks_presigned_urls_but_not_signed_headers(self):
        self.policy({"Sid": "NoPresign", "Effect": "Deny", "Principal": "*", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::shop-assets/*", "Condition": {"StringEquals": {"s3:authType": "REST-QUERY-STRING"}}})
        url = self.owner.presign("GET", "shop-assets", "public/logo.png")
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(self.raw("GET", parts.path + "?" + parts.query).error_code(), "AccessDenied")
        self.assertEqual(self.owner.get_object("shop-assets", "public/logo.png").status, 200)

    def test_s3_signature_age_limits_how_old_a_presigned_url_may_be(self):
        self.policy({"Sid": "TenMinutes", "Effect": "Deny", "Principal": {"AWS": "*"}, "Action": "s3:*", "Resource": "arn:aws:s3:::shop-assets/*", "Condition": {"NumericGreaterThan": {"s3:signatureAge": "600000"}}})
        url = self.owner.presign("GET", "shop-assets", "public/logo.png", expires=3600)
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(self.raw("GET", parts.path + "?" + parts.query).status, 200)
        self.clock.now = NOW + 601
        self.assertEqual(self.raw("GET", parts.path + "?" + parts.query).error_code(), "AccessDenied")
        self.assertEqual(self.owner.get_object("shop-assets", "public/logo.png").status, 200)

    def test_list_bucket_can_be_limited_with_s3_prefix(self):
        self.policy({"Effect": "Allow", "Principal": {"AWS": PARTNER.arn}, "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::shop-assets", "Condition": {"StringLike": {"s3:prefix": "public/*"}}})
        self.assertEqual(self.partner.list_objects_v2("shop-assets", prefix="public/")["keys"], ["public/logo.png"])
        with self.assertRaises(main.ClientError):
            self.partner.list_objects_v2("shop-assets", prefix="private/")

    def test_the_owner_root_can_always_remove_a_policy_that_denies_everyone(self):
        self.policy({"Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": ["arn:aws:s3:::shop-assets", "arn:aws:s3:::shop-assets/*"]})
        self.assertEqual(self.owner.call("DELETE", "shop-assets", query=[("policy", "")]).error_code(), "AccessDenied")
        self.assertEqual(self.root.call("DELETE", "shop-assets", query=[("policy", "")]).status, 204)
        self.assertEqual(self.owner.get_object("shop-assets", "private/keys.txt").status, 200)

    def test_a_policy_for_another_bucket_or_with_an_unknown_operator_is_malformedpolicy(self):
        foreign = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::other-bucket/*"}]}
        self.assertEqual(self.owner.put_policy("shop-assets", foreign).error_code(), "MalformedPolicy")
        odd = {"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": "*", "Condition": {"StringSorta": {"s3:prefix": "x"}}}]}
        self.assertEqual(self.owner.put_policy("shop-assets", odd).error_code(), "MalformedPolicy")


class CliTests(ServerCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_vectors_command_passes_on_the_shipped_fixtures(self):
        code, out, _ = self.run_cli("vectors", os.path.join(FIXTURES, "aws-sigv4-test-suite.json"), os.path.join(FIXTURES, "s3-api-examples.json"))
        self.assertEqual(code, 0)
        self.assertIn("81/81 signatures match", out)

    def test_presign_then_inspect_then_fetch_with_the_cli(self):
        url = f"{self.endpoint}/shop-assets/cli.txt"
        self.owner.put_object("shop-assets", "cli.txt", b"from the cli")
        common = ["--access-key", OWNER.access_key, "--secret-key", OWNER.secret_key, "--date", "20261101T120000Z"]
        code, out, _ = self.run_cli("presign", url, "--expires", "120", *common)
        self.assertEqual(code, 0)
        presigned = out.strip()
        code, out, _ = self.run_cli("inspect", presigned, "--secret-key", OWNER.secret_key, "--date", "20261101T120100Z", "--json")
        self.assertEqual((code, json.loads(out)["verified"]), (0, True))
        code, _, _ = self.run_cli("inspect", presigned, "--date", "20261101T121000Z")
        self.assertEqual(code, 1)
        parts = urllib.parse.urlsplit(presigned)
        self.assertEqual(self.raw("GET", parts.path + "?" + parts.query).body, b"from the cli")

    def test_sign_json_and_send_report_status_and_exit_codes(self):
        common = ["--access-key", OWNER.access_key, "--secret-key", OWNER.secret_key, "--date", "20261101T120000Z"]
        code, out, _ = self.run_cli("sign", "GET", f"{self.endpoint}/shop-assets?list-type=2&prefix=a%20b", *common, "--json")
        signed = json.loads(out)
        self.assertEqual(code, 0)
        self.assertIn("\nlist-type=2&prefix=a%20b\n", signed["canonical_request"])
        code, out, _ = self.run_cli("send", "GET", f"{self.endpoint}/shop-assets/missing.txt", *common, "--json")
        self.assertEqual((code, json.loads(out)["code"]), (1, "NoSuchKey"))
        self.assertEqual(self.run_cli("serve", "--key", "no-colon")[0], 2)


if __name__ == "__main__":
    unittest.main()
