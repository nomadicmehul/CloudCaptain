import itertools
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
VECTORS = json.loads((FIXTURES / "rfc8439-vectors.json").read_text())
SCENARIO = FIXTURES / "scenario.json"
H = bytes.fromhex


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def dev(**kwargs):
    clock = kwargs.pop("clock", None) or Clock()
    database = kwargs.pop("database", None) or main.FakeDatabase(clock=clock)
    vault, _ = main.Vault.dev("root", clock=clock, database=database, **kwargs)
    return vault, clock, database


def call(vault, method, path, body=None, token="root", query=None):
    response = vault.handle(main.Request(method, path, query or {}, body, token))
    return response.status, response.body


def token_for(vault, policies, **extra):
    status, body = call(vault, "POST", "auth/token/create", {"policies": policies, **extra})
    assert status == 200, body
    return body["auth"]["client_token"]


class ChaCha20Poly1305Tests(unittest.TestCase):
    def test_quarter_round_matches_section_2_1_1(self):
        vector = VECTORS["quarter_round"]
        self.assertEqual(main.quarter_round(*[int(x, 16) for x in vector["input"]]), tuple(int(x, 16) for x in vector["output"]))

    def test_block_function_matches_section_2_3_2_and_appendix_a1(self):
        for vector in [VECTORS["block"]] + VECTORS["block_appendix"]:
            with self.subTest(vector["section"]):
                self.assertEqual(main.chacha20_block(H(vector["key"]), vector["counter"], H(vector["nonce"])).hex(), vector["output"])

    def test_encryption_matches_section_2_4_2_and_appendix_a2(self):
        for vector in [VECTORS["encrypt"]] + VECTORS["encrypt_appendix"]:
            with self.subTest(vector["section"]):
                ciphertext = main.chacha20_xor(H(vector["key"]), vector["counter"], H(vector["nonce"]), H(vector["plaintext"]))
                self.assertEqual(ciphertext.hex(), vector["ciphertext"])
                self.assertEqual(main.chacha20_xor(H(vector["key"]), vector["counter"], H(vector["nonce"]), ciphertext).hex(), vector["plaintext"])

    def test_poly1305_matches_section_2_5_2_and_the_appendix_a3_edge_cases(self):
        for vector in [VECTORS["poly1305"]] + VECTORS["poly1305_appendix"]:
            with self.subTest(vector["section"]):
                self.assertEqual(main.poly1305_mac(H(vector["message"]), H(vector["key"])).hex(), vector["tag"])

    def test_one_time_key_generation_matches_section_2_6_2_and_appendix_a4(self):
        for vector in VECTORS["poly1305_keygen"]:
            with self.subTest(vector["section"]):
                self.assertEqual(main.poly1305_key_gen(H(vector["key"]), H(vector["nonce"])).hex(), vector["output"])

    def test_aead_matches_section_2_8_2_and_decrypts_appendix_a5(self):
        for vector in VECTORS["aead"]:
            with self.subTest(vector["section"]):
                sealed = main.aead_encrypt(H(vector["key"]), H(vector["nonce"]), H(vector["plaintext"]), H(vector["aad"]))
                self.assertEqual(sealed.hex(), vector["ciphertext"] + vector["tag"])
                self.assertEqual(main.aead_decrypt(H(vector["key"]), H(vector["nonce"]), sealed, H(vector["aad"])).hex(), vector["plaintext"])

    def test_aead_rejects_any_change_to_ciphertext_tag_aad_or_key(self):
        vector = VECTORS["aead"][0]
        key, nonce, aad = H(vector["key"]), H(vector["nonce"]), H(vector["aad"])
        sealed = main.aead_encrypt(key, nonce, H(vector["plaintext"]), aad)
        flipped = bytes([sealed[0] ^ 1]) + sealed[1:]
        bad_tag = sealed[:-1] + bytes([sealed[-1] ^ 0x80])
        for label, attempt in (("ciphertext", lambda: main.aead_decrypt(key, nonce, flipped, aad)), ("tag", lambda: main.aead_decrypt(key, nonce, bad_tag, aad)), ("aad", lambda: main.aead_decrypt(key, nonce, sealed, aad + b"x")), ("key", lambda: main.aead_decrypt(bytes(32), nonce, sealed, aad)), ("short", lambda: main.aead_decrypt(key, nonce, b"tiny", aad))):
            with self.subTest(label), self.assertRaises(main.DecryptionError):
                attempt()

    def test_the_block_counter_may_not_wrap(self):
        with self.assertRaises(ValueError):
            main.chacha20_xor(bytes(32), 0xFFFFFFFF, bytes(12), bytes(65))


class ShamirTests(unittest.TestCase):
    def test_field_arithmetic_matches_fips_197_and_every_element_has_an_inverse(self):
        self.assertEqual(main.gf_mul(0x57, 0x83), 0xC1)
        self.assertEqual(main.gf_mul(0x57, 0x13), 0xFE)
        self.assertEqual(main.gf_inverse(0x53), 0xCA)
        self.assertTrue(all(main.gf_mul(a, main.gf_inverse(a)) == 1 for a in range(1, 256)))
        with self.assertRaises(ZeroDivisionError):
            main.gf_inverse(0)

    def test_a_hand_computed_polynomial_is_interpolated_back_to_its_constant_term(self):
        coefficients = [0x42, 0x01, 0x02]
        ys = [main.gf_poly_eval(coefficients, x) for x in (1, 2, 3)]
        self.assertEqual(ys, [0x41, 0x48, 0x4B])
        shares = [bytes([y, x]) for y, x in zip(ys, (1, 2, 3))]
        self.assertEqual(main.shamir_combine(shares), b"\x42")

    def test_any_threshold_subset_recovers_the_secret_and_fewer_do_not(self):
        secret = bytes(range(32))
        shares = main.shamir_split(secret, 5, 3)
        self.assertEqual(len({s[-1] for s in shares}), 5)
        self.assertTrue(all(len(s) == 33 and s[-1] != 0 for s in shares))
        for subset in itertools.combinations(shares, 3):
            self.assertEqual(main.shamir_combine(list(subset)), secret)
        self.assertEqual(main.shamir_combine(shares), secret)
        self.assertNotEqual(main.shamir_combine(shares[:2]), secret)

    def test_split_and_combine_refuse_what_openbao_refuses(self):
        for args, message in (((b"k", 2, 3), "less than threshold"), ((b"k", 256, 3), "exceed 255"), ((b"k", 3, 1), "at least 2"), ((b"", 3, 2), "empty")):
            with self.subTest(message), self.assertRaisesRegex(main.InputError, message):
                main.shamir_split(*args)
        a, b = main.shamir_split(b"secret", 3, 2)[:2]
        with self.assertRaisesRegex(main.InputError, "duplicate"):
            main.shamir_combine([a, a])
        with self.assertRaisesRegex(main.InputError, "same length"):
            main.shamir_combine([a, b[:-2] + b[-1:]])
        with self.assertRaisesRegex(main.InputError, "less than two"):
            main.shamir_combine([a])


class EnvelopeTests(unittest.TestCase):
    def test_each_value_gets_its_own_data_key_bound_to_its_path(self):
        ring = main.Keyring.generate()
        first = ring.seal(b'{"password": "pw"}', "secret/app#v1")
        second = ring.seal(b'{"password": "pw"}', "secret/app#v1")
        self.assertNotEqual(first["ciphertext"], second["ciphertext"])
        self.assertEqual(ring.open(first, "secret/app#v1"), b'{"password": "pw"}')
        with self.assertRaises(main.DecryptionError):
            ring.open(first, "secret/other#v1")

    def test_rotation_keeps_old_terms_and_rewrap_changes_only_the_wrapped_key(self):
        ring = main.Keyring.generate()
        record = ring.seal(b"data", "x#v1")
        self.assertEqual(ring.rotate(), 2)
        self.assertEqual(ring.open(record, "x#v1"), b"data")
        rewrapped = ring.rewrap(record, "x#v1")
        self.assertEqual((rewrapped["term"], rewrapped["ciphertext"]), (2, record["ciphertext"]))
        self.assertNotEqual(rewrapped["wrapped_key"], record["wrapped_key"])
        del ring.terms[1]
        self.assertEqual(ring.open(rewrapped, "x#v1"), b"data")
        with self.assertRaisesRegex(main.DecryptionError, "no term 1"):
            ring.open(record, "x#v1")


class PolicyTests(unittest.TestCase):
    def acl(self, *policies):
        rules = main.compile_rules("default", main.DEFAULT_POLICY)
        for name, text in policies:
            rules += main.compile_rules(name, text)
        return main.ACL(rules)

    def test_hcl_with_comments_and_json_policies_parse_to_the_same_rules(self):
        hcl = '# comment\npath "secret/data/app/*" {\n  // another\n  capabilities = ["read", "list",]\n}\n'
        js = '{"path": {"secret/data/app/*": {"capabilities": ["read", "list"]}}}'
        self.assertEqual(main.compile_rules("p", hcl), main.compile_rules("p", js))
        rule = main.compile_rules("p", hcl)[0]
        self.assertEqual((rule.kind, rule.key, sorted(rule.capabilities)), ("prefix", "secret/data/app/", ["list", "read"]))
        self.assertEqual(main.compile_rules("p", 'path "secret/x" { policy = "write" }')[0].capabilities, frozenset(["create", "read", "update", "delete", "list"]))

    def test_invalid_policies_name_the_problem(self):
        for text, message in (('path "a/*" { capabilities = ["fly"] }', "invalid capability"), ('path "a/+*" { capabilities = ["read"] }', r"\+\*"), ('path "a" { allowed_parameters = "x" }', "only 'capabilities'"), ('key "a" {}', "expected 'path'"), ('path "a" { capabilities = ["read"]', "unexpected end")):
            with self.subTest(message), self.assertRaisesRegex(main.InputError, message):
                main.compile_rules("p", text)

    def test_matching_follows_openbaos_precedence(self):
        scenario = json.loads(SCENARIO.read_text())
        text = next(s["body"]["policy"] for s in scenario["scenarios"]["policy"] if s["path"] == "sys/policies/acl/app")
        ops = next(s["body"]["policy"] for s in scenario["scenarios"]["policy"] if s["path"] == "sys/policies/acl/ops")
        acl = self.acl(("app", text))
        expected = {
            "secret/data/app/config": ["read", "update", "create"],
            "secret/data/app/admin": ["deny"],
            "secret/data/app/shared": ["read", "update", "create"],
            "secret/data/team/shared": ["read"],
            "secret/data/team/db": ["deny"],
            "secret/data/app": ["deny"],
            "secret/metadata/app/": ["read", "list"],
            "secret/metadata/app": ["deny"],
            "cubbyhole/notes": ["read", "list", "update", "delete", "create"],
        }
        for path, capabilities in expected.items():
            with self.subTest(path):
                self.assertEqual(acl.capabilities(path), capabilities)
        both = self.acl(("app", text), ("ops", ops))
        self.assertEqual(both.capabilities("secret/data/app/config"), ["read", "update", "delete", "create"])
        self.assertEqual(both.capabilities("secret/data/app/admin"), ["deny"])
        self.assertTrue(acl.allows("secret/metadata/app/", "list"))
        self.assertFalse(acl.allows("secret/data/app/config", "delete"))
        self.assertEqual(main.ACL([], root=True).capabilities("anything"), ["root"])

    def test_explain_names_the_rule_that_decided(self):
        acl = self.acl(("app", 'path "secret/data/app/*" { capabilities = ["read"] }\npath "secret/data/app/admin" { capabilities = ["deny"] }\npath "auth/token/revoke-orphan" { capabilities = ["update"] }'))
        denied = acl.explain("secret/data/app/admin", "read")
        self.assertFalse(denied["allowed"])
        self.assertIn("deny overrides", denied["reason"])
        self.assertEqual(denied["rule"]["patterns"], ["secret/data/app/admin"])
        self.assertIn("no exact, glob or + rule", acl.explain("secret/data/other", "read")["reason"])
        self.assertIn("does not grant update", acl.explain("secret/data/app/x", "update")["reason"])
        self.assertIn("needs sudo", acl.explain("auth/token/revoke-orphan", "update")["reason"])


class KVTests(unittest.TestCase):
    def test_versions_check_and_set_soft_delete_undelete_and_destroy(self):
        vault, clock, _ = dev()
        path = "secret/data/app/config"
        for n in (1, 2):
            status, body = call(vault, "POST", path, {"data": {"password": f"pw-{n}"}})
            self.assertEqual((status, body["data"]["version"]), (200, n))
        status, body = call(vault, "POST", path, {"data": {"password": "x"}, "options": {"cas": 1}})
        self.assertEqual(status, 400)
        self.assertIn("check-and-set parameter did not match", body["errors"][0])
        self.assertEqual(call(vault, "GET", path, query={"version": "1"})[1]["data"]["data"], {"password": "pw-1"})
        self.assertEqual(call(vault, "DELETE", path)[0], 204)
        status, body = call(vault, "GET", path)
        self.assertEqual(status, 404)
        self.assertIsNone(body["data"]["data"])
        self.assertTrue(body["data"]["metadata"]["deletion_time"])
        self.assertEqual(call(vault, "POST", "secret/undelete/app/config", {"versions": [2]})[0], 204)
        self.assertEqual(call(vault, "GET", path)[1]["data"]["data"], {"password": "pw-2"})
        self.assertEqual(call(vault, "POST", "secret/destroy/app/config", {"versions": [1]})[0], 204)
        status, body = call(vault, "GET", path, query={"version": "1"})
        self.assertEqual((status, body["data"]["metadata"]["destroyed"]), (404, True))
        self.assertIsNone(vault.mounts["secret/"]["keys"]["app/config"]["versions"]["1"]["record"])
        self.assertEqual(call(vault, "POST", "secret/delete/app/config", {})[0], 400)

    def test_max_versions_trims_the_oldest_like_openbao(self):
        vault, _, _ = dev()
        call(vault, "POST", "secret/metadata/r", {"max_versions": 2})
        for n in range(1, 5):
            call(vault, "POST", "secret/data/r", {"data": {"n": n}})
        meta = call(vault, "GET", "secret/metadata/r")[1]["data"]
        self.assertEqual((meta["current_version"], meta["oldest_version"], sorted(meta["versions"])), (4, 3, ["3", "4"]))
        self.assertEqual(call(vault, "GET", "secret/data/r", query={"version": "2"}), (404, {"errors": []}))

    def test_list_returns_children_and_folders(self):
        vault, _, _ = dev()
        for key in ("app/a", "app/b/c", "team/x"):
            call(vault, "POST", f"secret/data/{key}", {"data": {"k": "v"}})
        self.assertEqual(call(vault, "LIST", "secret/metadata/")[1]["data"]["keys"], ["app/", "team/"])
        self.assertEqual(call(vault, "LIST", "secret/metadata/app")[1]["data"]["keys"], ["a", "b/"])
        self.assertEqual(call(vault, "GET", "secret/metadata/app", query={"list": "true"})[1]["data"]["keys"], ["a", "b/"])
        self.assertEqual(call(vault, "LIST", "secret/metadata/none")[0], 404)

    def test_secret_values_and_tokens_never_reach_the_state_file_in_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = main.Vault(data_dir=tmp)
            init = vault.initialize(3, 2)
            for share in init["keys"][:2]:
                vault.submit_unseal_key(share)
            call(vault, "POST", "secret/data/db", {"data": {"password": "hunter2-plaintext"}}, token=init["root_token"])
            child = call(vault, "POST", "auth/token/create", {"policies": ["default"]}, token=init["root_token"])[1]["auth"]["client_token"]
            state = (Path(tmp) / main.STATE_FILE).read_text()
            self.assertNotIn("hunter2-plaintext", state)
            self.assertNotIn(init["root_token"], state)
            self.assertNotIn(child, state)
            self.assertEqual(stat.S_IMODE(os.stat(Path(tmp) / main.STATE_FILE).st_mode), 0o600)


class TokenTests(unittest.TestCase):
    def test_a_token_stops_working_at_its_ttl(self):
        vault, clock, _ = dev()
        call(vault, "POST", "secret/data/a", {"data": {"k": "v"}})
        token = token_for(vault, ["default"], ttl="3s")
        self.assertEqual(call(vault, "GET", "auth/token/lookup-self", token=token)[1]["data"]["ttl"], 3)
        clock.advance(2.9)
        self.assertEqual(call(vault, "GET", "auth/token/lookup-self", token=token)[0], 200)
        clock.advance(0.2)
        self.assertEqual(call(vault, "GET", "auth/token/lookup-self", token=token), (403, {"errors": ["permission denied"]}))

    def test_renewal_is_capped_by_explicit_max_ttl_and_refused_when_not_renewable(self):
        vault, clock, _ = dev()
        token = token_for(vault, ["default"], ttl="10m", explicit_max_ttl="1h")
        self.assertEqual(call(vault, "POST", "auth/token/renew-self", {"increment": "30m"}, token=token)[1]["auth"]["lease_duration"], 1800)
        clock.advance(600)
        status, body = call(vault, "POST", "auth/token/renew-self", {"increment": "2h"}, token=token)
        self.assertEqual((status, body["auth"]["lease_duration"]), (200, 3000))
        self.assertIn("capped", body["warnings"][0])
        fixed = token_for(vault, ["default"], ttl="10m", renewable=False)
        self.assertEqual(call(vault, "POST", "auth/token/renew-self", {}, token=fixed)[0], 400)
        self.assertEqual(call(vault, "POST", "auth/token/create", {"policies": ["default"], "ttl": "2h", "explicit_max_ttl": "1h"})[1]["auth"]["lease_duration"], 3600)

    def test_revoking_a_parent_revokes_its_children_and_revoke_orphan_does_not(self):
        vault, _, _ = dev()
        call(vault, "PUT", "sys/policies/acl/parent", {"policy": 'path "auth/token/create" { capabilities = ["update"] }'})
        parent = token_for(vault, ["parent"])
        status, body = call(vault, "POST", "auth/token/create", {"policies": ["parent"]}, token=parent)
        child = body["auth"]["client_token"]
        grandchild = call(vault, "POST", "auth/token/create", {}, token=child)[1]["auth"]["client_token"]
        self.assertEqual(call(vault, "POST", "auth/token/create", {"policies": ["root"]}, token=parent)[0], 400)
        self.assertIn("subset", call(vault, "POST", "auth/token/create", {"policies": ["other"]}, token=parent)[1]["errors"][0])
        self.assertEqual(call(vault, "POST", "auth/token/revoke", {"token": parent})[0], 204)
        for token in (parent, child, grandchild):
            self.assertEqual(call(vault, "GET", "auth/token/lookup-self", token=token)[0], 403)
        parent = token_for(vault, ["parent"])
        child = call(vault, "POST", "auth/token/create", {}, token=parent)[1]["auth"]["client_token"]
        self.assertEqual(call(vault, "POST", "auth/token/revoke-orphan", {"token": parent})[0], 204)
        status, body = call(vault, "GET", "auth/token/lookup-self", token=child)
        self.assertEqual((status, body["data"]["orphan"]), (200, True))
        self.assertEqual(call(vault, "POST", "auth/token/revoke-orphan", {"token": child}, token=child)[0], 403)

    def test_a_use_limited_token_is_revoked_after_its_last_use(self):
        vault, _, _ = dev()
        token = token_for(vault, ["default"], num_uses=2)
        self.assertEqual(call(vault, "GET", "auth/token/lookup-self", token=token)[1]["data"]["num_uses"], 1)
        self.assertEqual(call(vault, "GET", "auth/token/lookup-self", token=token)[0], 200)
        self.assertEqual(call(vault, "GET", "auth/token/lookup-self", token=token)[0], 403)


class SealTests(unittest.TestCase):
    def test_three_of_five_shares_unseal_and_two_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = main.Vault(data_dir=tmp)
            init = vault.initialize(5, 3)
            self.assertEqual((len(init["keys"]), len(init["keys"][0]) // 2), (5, 33))
            self.assertEqual(call(vault, "GET", "secret/data/x", token=init["root_token"])[0], 503)
            self.assertEqual(vault.submit_unseal_key(init["keys"][0])["progress"], 1)
            self.assertTrue(vault.submit_unseal_key(init["keys_base64"][1])["sealed"])
            self.assertFalse(vault.submit_unseal_key(init["keys"][2])["sealed"])
            call(vault, "POST", "secret/data/db", {"data": {"password": "pw"}}, token=init["root_token"])
            reopened = main.Vault(data_dir=tmp)
            self.assertTrue(reopened.seal_status()["sealed"])
            for share in (init["keys"][4], init["keys"][1], init["keys"][3]):
                reopened.submit_unseal_key(share)
            self.assertEqual(call(reopened, "GET", "secret/data/db", token=init["root_token"])[1]["data"]["data"], {"password": "pw"})

    def test_shares_from_another_init_fail_and_reset_progress(self):
        first, second = main.Vault(), main.Vault()
        a, b = first.initialize(3, 2), second.initialize(3, 2)
        first.submit_unseal_key(a["keys"][0])
        with self.assertRaises(main.ApiError) as raised:
            first.submit_unseal_key(b["keys"][1])
        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(first.seal_status()["progress"], 0)
        self.assertTrue(first.sealed)

    def test_a_recombined_and_resplit_key_unseals_the_same_data(self):
        vault = main.Vault()
        init = vault.initialize(5, 3)
        unseal_key = main.shamir_combine([bytes.fromhex(k) for k in init["keys"][2:]])
        self.assertEqual(unseal_key, main.shamir_combine([bytes.fromhex(k) for k in init["keys"][:3]]))
        fresh = main.shamir_split(unseal_key, 5, 3)
        self.assertFalse(set(fresh) & {bytes.fromhex(k) for k in init["keys"]})
        vault.submit_unseal_key(fresh[3].hex())
        vault.submit_unseal_key(fresh[1].hex())
        self.assertFalse(vault.submit_unseal_key(main.b64e(fresh[0]))["sealed"])

    def test_the_http_unseal_flow_and_seal(self):
        vault = main.Vault()
        init = call(vault, "POST", "sys/init", {"secret_shares": 3, "secret_threshold": 2})[1]
        self.assertEqual(call(vault, "PUT", "sys/unseal", {"key": init["keys"][0]})[1]["progress"], 1)
        self.assertEqual(call(vault, "PUT", "sys/unseal", {"reset": True})[1]["progress"], 0)
        call(vault, "PUT", "sys/unseal", {"key": init["keys"][0]})
        self.assertFalse(call(vault, "PUT", "sys/unseal", {"key": init["keys"][1]})[1]["sealed"])
        self.assertEqual(call(vault, "GET", "sys/health")[0], 200)
        self.assertEqual(call(vault, "POST", "sys/seal", {}, token=init["root_token"])[0], 204)
        self.assertEqual(call(vault, "GET", "sys/health")[0], 503)


class LeaseTests(unittest.TestCase):
    def setup_database(self, vault):
        call(vault, "POST", "sys/mounts/database", {"type": "database"})
        call(vault, "POST", "database/config/pg", {"plugin_name": "postgresql-database-plugin", "allowed_roles": ["ro"]})
        create = ["CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT SELECT ON ALL TABLES IN SCHEMA public TO \"{{name}}\";"]
        self.assertEqual(call(vault, "POST", "database/roles/ro", {"db_name": "pg", "creation_statements": create, "default_ttl": "1h", "max_ttl": "2h"})[0], 204)

    def test_a_credential_is_a_user_that_lives_as_long_as_its_lease(self):
        vault, clock, database = dev()
        self.setup_database(vault)
        status, body = call(vault, "GET", "database/creds/ro")
        self.assertEqual((status, body["lease_duration"], body["renewable"]), (200, 3600, True))
        user, password, lease = body["data"]["username"], body["data"]["password"], body["lease_id"]
        self.assertTrue(user.startswith("v-token-ro-") and lease.startswith("database/creds/ro/"))
        self.assertEqual(database.login(user, password)[0], True)
        self.assertFalse(database.login(user, "wrong")[0])
        clock.advance(3000)
        self.assertEqual(call(vault, "PUT", "sys/leases/renew", {"lease_id": lease, "increment": 7200})[1]["lease_duration"], 4200)
        clock.advance(4000)
        self.assertTrue(database.login(user, password)[0])
        clock.advance(201)
        vault.tick()
        self.assertFalse(database.login(user, password)[0])
        self.assertEqual(call(vault, "PUT", "sys/leases/lookup", {"lease_id": lease})[0], 400)

    def test_revoking_the_lease_or_its_token_drops_the_user(self):
        vault, _, database = dev()
        self.setup_database(vault)
        body = call(vault, "GET", "database/creds/ro")[1]
        self.assertEqual(call(vault, "PUT", "sys/leases/revoke", {"lease_id": body["lease_id"]})[0], 204)
        self.assertFalse(database.login(body["data"]["username"], body["data"]["password"])[0])
        call(vault, "PUT", "sys/policies/acl/db", {"policy": 'path "database/creds/ro" { capabilities = ["read"] }'})
        token = token_for(vault, ["db"], display_name="Orders API")
        body = call(vault, "GET", "database/creds/ro", token=token)[1]
        self.assertEqual(call(vault, "GET", "auth/token/lookup-self", token=token)[1]["data"]["display_name"], "token-Orders-API")
        self.assertTrue(body["data"]["username"].startswith("v-token-Or-ro-"))
        self.assertTrue(database.login(body["data"]["username"], body["data"]["password"])[0])
        call(vault, "POST", "auth/token/revoke", {"token": token})
        self.assertFalse(database.login(body["data"]["username"], body["data"]["password"])[0])
        self.assertEqual(vault.leases, {})

    def test_the_command_database_pipes_statements_and_redacts_passwords(self):
        seen = {}

        def runner(argv, **kwargs):
            seen.update(argv=argv, script=kwargs["input"])
            return SimpleNamespace(returncode=3, stdout="", stderr="ERROR: syntax error\nLINE 1: CREATE ROLE \"u\" WITH LOGIN PASSWORD 'TopSecret1' VALID")

        database = main.CommandDatabase(["docker", "exec", "-i", "pg", "psql"], runner)
        statements = main.render_statements(main.split_statements("CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}'; GRANT x TO \"{{name}}\""), name="u", password="TopSecret1")
        self.assertEqual(statements, ["CREATE ROLE \"u\" WITH LOGIN PASSWORD 'TopSecret1';", 'GRANT x TO "u";'])
        with self.assertRaises(main.EngineError) as raised:
            database.execute(statements)
        self.assertEqual(seen["argv"][-1], "psql")
        self.assertIn("TopSecret1", seen["script"])
        self.assertNotIn("TopSecret1", str(raised.exception))
        self.assertIn("exited 3", str(raised.exception))


class AuditTests(unittest.TestCase):
    def test_the_audit_log_holds_hmacs_that_audit_hash_can_reproduce(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "audit.log"
            vault, _, _ = dev(audit_path=str(log))
            token = token_for(vault, ["default"])
            call(vault, "POST", "secret/data/probe", {"data": {"api_key": "plaintext-value-123"}})
            call(vault, "GET", "secret/data/probe")
            digest = call(vault, "POST", "sys/audit-hash/file", {"input": "plaintext-value-123"})[1]["data"]["hash"]
            text = log.read_text()
            self.assertNotIn("plaintext-value-123", text)
            self.assertNotIn(token, text)
            self.assertGreaterEqual(text.count(digest), 3)
            entries = [json.loads(line) for line in text.splitlines()]
            self.assertEqual({e["type"] for e in entries}, {"request", "response"})
            self.assertEqual(entries[0]["auth"]["client_token"], vault.audit.hash("root"))


class ReplayTests(unittest.TestCase):
    def test_filters_and_variables(self):
        body = {"data": {"keys": ["b", "a"], "versions": {"2": {}, "1": {}}, "ttl": 3599, "id": "database/creds/ro/x"}, "errors": ["check-and-set parameter did not match"]}
        self.assertEqual(main.observe(body, "data.keys|sorted"), ["a", "b"])
        self.assertEqual(main.observe(body, "data.versions|keys"), ["1", "2"])
        self.assertEqual([main.observe(body, s) for s in ("data.ttl|le:3600", "data.ttl|ge:3600", "data.id|startswith:database/", "errors.0|contains:check-and-set", "data.missing|set")], [True, False, True, True, False])
        self.assertEqual(main.substitute({"a": "${x}", "b": "t-${y}"}, {"x": {"obj": 1}, "y": 2}), {"a": {"obj": 1}, "b": "t-2"})
        with self.assertRaisesRegex(main.InputError, "not set"):
            main.substitute("${nope}", {})

    def test_the_shared_scenario_passes_against_minivault_over_http(self):
        vault, _ = main.Vault.dev("root")
        server = main.VaultServer(("127.0.0.1", 0), vault)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        address = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                vault.audit.path = Path(tmp) / "audit.log"
                code, out, err = run(["replay", "--addr", address, "--script", SCENARIO, "--json", "--scenario", "kv", "--scenario", "policy", "--scenario", "db-setup", "--scenario", "db-issue", "--scenario", "db-revoke", "--scenario", "audit", "--var", 'db_config={"plugin_name": "postgresql-database-plugin", "allowed_roles": "readonly,short"}', "--var", "probe=p-1"])
            report = json.loads(out)
            self.assertEqual(code, 0, [s for s in report["steps"] if not s["ok"]])
            self.assertEqual(len(report["steps"]), 71)
        finally:
            server.shutdown()
            server.server_close()


class CliTests(unittest.TestCase):
    def test_shamir_split_and_combine_round_trip(self):
        code, out, _ = run(["shamir", "split", "--hex", "00112233445566778899aabbccddeeff", "--shares", "4", "--threshold", "2", "--json"])
        shares = json.loads(out)["shares_b64"]
        code2, out2, _ = run(["shamir", "combine", shares[3], shares[1]])
        self.assertEqual((code, code2, out2.strip()), (0, 0, "00112233445566778899aabbccddeeff"))
        self.assertEqual(run(["shamir", "combine", shares[0]])[0], 2)

    def test_policy_check_explains_and_exits_1_on_deny(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "app.hcl"
            policy.write_text('path "secret/data/app/*" { capabilities = ["read"] }\n')
            code, out, _ = run(["policy", "check", "--policy", f"app={policy}", "--path", "secret/data/app/db", "--json"])
            self.assertEqual((code, json.loads(out)[0]["capabilities"]), (0, ["read"]))
            code, out, _ = run(["policy", "check", "--policy", f"app={policy}", "--path", "secret/data/app/db", "--operation", "update"])
            self.assertEqual(code, 1)
            self.assertIn("DENY", out)

    def test_operator_init_writes_a_private_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run(["operator", "init", "--data", tmp, "--shares", "5", "--threshold", "3", "--json"])
            result = json.loads(out)
            self.assertEqual((code, len(result["unseal_keys_b64"]), result["unseal_threshold"]), (0, 5, 3))
            self.assertEqual(stat.S_IMODE(os.stat(Path(tmp) / main.STATE_FILE).st_mode), 0o600)
            self.assertEqual(run(["operator", "init", "--data", tmp])[0], 2)

    def test_bad_input_exits_2_with_a_reason(self):
        code, _, err = run(["replay", "--addr", "http://127.0.0.1:9", "--script", SCENARIO, "--scenario", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("unknown scenario", err)
        self.assertEqual(main.parse_duration("1h30m"), 5400)
        with self.assertRaises(main.InputError):
            main.parse_duration("soon")


if __name__ == "__main__":
    unittest.main()
