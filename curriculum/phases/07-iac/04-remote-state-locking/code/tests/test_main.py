import base64
import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


def state_bytes(serial, lineage="lineage-a", content="v1"):
    state = {"version": 4, "terraform_version": "1.13.2", "serial": serial, "lineage": lineage, "outputs": {}, "resources": [{"type": "local_file", "name": "motd", "content": content}], "check_results": None}
    return json.dumps(state, indent=2).encode("utf-8")


def lock_body(lock_id, who="alice@laptop", operation="OperationTypeApply"):
    return json.dumps({"ID": lock_id, "Operation": operation, "Info": "", "Who": who, "Version": "1.13.2", "Created": "2026-11-01T09:00:00Z", "Path": ""}).encode("utf-8")


class ServerCase(unittest.TestCase):
    credentials = None
    require_lock = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = main.StateStore(os.path.join(self.tmp.name, "data"))
        self.server = main.make_server(self.store, "127.0.0.1", 0, self.credentials, self.require_lock)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.address = f"http://127.0.0.1:{self.port}/states/demo"
        user, password = self.credentials or (None, None)
        self.client = main.StateClient(self.address, username=user, password=password)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def raw(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()


class LockProtocolTests(ServerCase):
    def test_second_lock_gets_423_with_the_holders_lock_info(self):
        status, _, _ = self.raw("LOCK", "/states/demo", lock_body("id-a", who="alice@ci"))
        self.assertEqual(status, 200)
        status, headers, body = self.raw("LOCK", "/states/demo", lock_body("id-b", who="bob@laptop"))
        self.assertEqual(status, 423)
        holder = json.loads(body)
        self.assertEqual(holder["ID"], "id-a")
        self.assertEqual(holder["Who"], "alice@ci")
        self.assertIn("id-a", headers["X-Statesrv-Error"])

    def test_lock_subresource_accepts_post_and_delete_like_gitlab(self):
        status, _, _ = self.raw("POST", "/states/demo/lock", lock_body("id-a"))
        self.assertEqual(status, 200)
        status, _, body = self.raw("GET", "/states/demo/lock")
        self.assertEqual((status, json.loads(body)["ID"]), (200, "id-a"))
        status, _, _ = self.raw("DELETE", "/states/demo/lock", json.dumps({"ID": "id-a"}).encode())
        self.assertEqual(status, 200)
        status, _, _ = self.raw("GET", "/states/demo/lock")
        self.assertEqual(status, 404)

    def test_unlock_with_the_wrong_id_is_409_and_the_lock_survives(self):
        self.raw("LOCK", "/states/demo", lock_body("id-a"))
        status, _, body = self.raw("UNLOCK", "/states/demo", lock_body("id-b"))
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["ID"], "id-a")
        self.assertEqual(self.store.lock_info("demo")["ID"], "id-a")

    def test_unlock_with_only_the_id_releases_and_is_recorded_like_force_unlock(self):
        self.raw("LOCK", "/states/demo", lock_body("id-a"))
        forced = {"ID": "id-a", "Operation": "", "Info": "", "Who": "", "Version": "", "Created": "0001-01-01T00:00:00Z", "Path": ""}
        status, _, _ = self.raw("UNLOCK", "/states/demo", json.dumps(forced).encode())
        self.assertEqual(status, 200)
        self.assertIsNone(self.store.lock_info("demo"))
        unlock = [event for event in self.store.events("demo") if event["event"] == "unlock"][-1]
        self.assertIn("force-unlock", unlock["released_by"])
        self.assertEqual(unlock["who"], "alice@laptop")

    def test_lock_is_exclusive_across_two_store_instances_on_one_directory(self):
        other = main.StateStore(self.store.root)
        self.store.lock("demo", {"ID": "id-a", "Who": "server-1"})
        with self.assertRaises(main.StoreError) as caught:
            other.lock("demo", {"ID": "id-b", "Who": "server-2"})
        self.assertEqual(caught.exception.status, 423)
        self.assertEqual(caught.exception.lock["ID"], "id-a")


class WriteTests(ServerCase):
    def test_get_of_a_missing_state_is_404_and_the_client_sees_none(self):
        status, _, _ = self.raw("GET", "/states/demo")
        self.assertEqual(status, 404)
        self.assertIsNone(self.client.get())

    def test_first_write_is_version_1_and_get_returns_the_same_bytes(self):
        body = state_bytes(1)
        entry = self.client.put_bytes(body)
        self.assertEqual((entry["version"], entry["serial"], entry["lineage"]), (1, 1, "lineage-a"))
        status, headers, served = self.raw("GET", "/states/demo")
        self.assertEqual((status, served), (200, body))
        self.assertEqual(headers["Content-MD5"], main.md5_b64(body))

    def test_write_under_a_lock_needs_the_matching_id(self):
        lock = self.client.lock(who="alice@ci")
        with self.assertRaises(main.HTTPStatusError) as no_id:
            self.client.put_bytes(state_bytes(1))
        self.assertEqual(no_id.exception.status, 423)
        with self.assertRaises(main.HTTPStatusError) as wrong_id:
            self.client.put_bytes(state_bytes(1), lock_id="someone-else")
        self.assertEqual(wrong_id.exception.status, 409)
        entry = self.client.put_bytes(state_bytes(1), lock_id=lock["ID"])
        self.assertEqual(entry["who"], "alice@ci")
        self.assertEqual(entry["lock_id"], lock["ID"])

    def test_a_write_with_a_lock_id_that_was_force_unlocked_is_refused(self):
        lock = self.client.lock(who="slow-apply@ci")
        self.store.force_unlock("demo")
        with self.assertRaises(main.HTTPStatusError) as caught:
            self.client.put_bytes(state_bytes(1), lock_id=lock["ID"])
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("not held", str(caught.exception))
        self.assertIsNone(self.store.read("demo"))
        self.assertEqual([e["event"] for e in self.store.events("demo")], ["lock", "force-unlock", "write-refused"])

    def test_an_older_serial_is_refused(self):
        self.client.put_bytes(state_bytes(3))
        with self.assertRaises(main.HTTPStatusError) as caught:
            self.client.put_bytes(state_bytes(2, content="stale"))
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("older than the stored serial 3", str(caught.exception))

    def test_a_different_lineage_is_refused_even_with_a_higher_serial(self):
        self.client.put_bytes(state_bytes(1))
        with self.assertRaises(main.HTTPStatusError) as caught:
            self.client.put_bytes(state_bytes(9, lineage="lineage-b"))
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("lineage", str(caught.exception))
        self.assertEqual(json.loads(self.store.read("demo"))["lineage"], "lineage-a")

    def test_the_same_serial_and_bytes_is_an_idempotent_retry(self):
        self.client.put_bytes(state_bytes(1))
        entry = self.client.put_bytes(state_bytes(1))
        self.assertTrue(entry["unchanged"])
        self.assertEqual(len(self.store.versions("demo")), 1)

    def test_the_same_serial_with_different_content_is_refused(self):
        self.client.put_bytes(state_bytes(1))
        with self.assertRaises(main.HTTPStatusError) as caught:
            self.client.put_bytes(state_bytes(1, content="other"))
        self.assertEqual(caught.exception.status, 409)

    def test_history_keeps_every_version_and_serves_each_body(self):
        for serial in (1, 2, 3):
            self.client.put_bytes(state_bytes(serial, content=f"v{serial}"))
        versions = self.client.versions()
        self.assertEqual([v["serial"] for v in versions], [1, 2, 3])
        self.assertEqual({v["lineage"] for v in versions}, {"lineage-a"})
        self.assertEqual(self.client.version(2), state_bytes(2, content="v2"))
        status, _, _ = self.raw("GET", "/states/demo/versions/9")
        self.assertEqual(status, 404)

    def test_a_corrupted_upload_is_rejected_by_content_md5(self):
        body = state_bytes(1)
        status, _, response = self.raw("POST", "/states/demo", body, {"Content-MD5": main.md5_b64(b"something else"), "Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn("Content-MD5", json.loads(response)["error"])

    def test_non_state_json_is_400_and_an_encrypted_envelope_is_stored_as_opaque(self):
        status, _, _ = self.raw("POST", "/states/demo", b'{"hello": "world"}')
        self.assertEqual(status, 400)
        envelope = json.dumps({"meta": {"key_provider.pbkdf2.main": "c2FsdA=="}, "encrypted_data": "AAAA", "encryption_version": "v0"}).encode()
        entry = self.client.put_bytes(envelope)
        self.assertTrue(entry["opaque"])
        self.assertIsNone(entry["serial"])

    def test_delete_needs_the_lock_and_keeps_history(self):
        self.client.put_bytes(state_bytes(1))
        lock = self.client.lock()
        status, _, _ = self.raw("DELETE", "/states/demo")
        self.assertEqual(status, 423)
        status, _, _ = self.raw("DELETE", f"/states/demo?ID={lock['ID']}")
        self.assertEqual(status, 200)
        self.assertIsNone(self.client.get())
        self.assertEqual(len(self.client.versions()), 1)

    def test_bad_names_paths_and_methods(self):
        self.assertEqual(self.raw("GET", "/states/..%2Fetc")[0], 400)
        self.assertEqual(self.raw("GET", "/nothing")[0], 404)
        self.assertEqual(self.raw("PATCH", "/states/demo")[0], 501)
        self.assertEqual(self.raw("LOCK", "/states/demo/versions")[0], 404)
        self.assertEqual(self.raw("PUT", "/states/demo/lock", b"not json")[0], 400)


class ConcurrencyTests(ServerCase):
    def test_two_concurrent_applies_the_second_fails_naming_the_first_lock(self):
        holding = threading.Event()
        release = threading.Event()
        results = {}

        def slow_change(state):
            state["resources"].append({"type": "local_file", "name": "a"})
            holding.set()
            release.wait(10)

        def first():
            results["first"] = main.locked_apply(main.StateClient(self.address), slow_change, who="alice@ci")

        worker = threading.Thread(target=first)
        worker.start()
        self.assertTrue(holding.wait(10))
        holder = self.client.lock_status()
        with self.assertRaises(main.LockedError) as caught:
            main.locked_apply(self.client, lambda state: None, who="bob@laptop")
        self.assertEqual(caught.exception.holder["ID"], holder["ID"])
        self.assertEqual(caught.exception.holder["Who"], "alice@ci")
        self.assertIn(f"ID={holder['ID']}", str(caught.exception))
        release.set()
        worker.join(10)
        self.assertEqual(results["first"]["serial"], 1)
        second = main.locked_apply(self.client, lambda state: state["resources"].append({"type": "local_file", "name": "b"}), who="bob@laptop")
        self.assertEqual(second["serial"], 2)
        self.assertEqual([v["who"] for v in self.client.versions()], ["alice@ci", "bob@laptop"])
        self.assertIsNone(self.client.lock_status())

    def test_without_a_lock_the_lost_update_is_caught_by_the_serial_check(self):
        main.locked_apply(self.client, lambda state: None, who="setup")
        alice = self.client.get()
        bob = self.client.get()
        alice["serial"] += 1
        alice["resources"].append({"type": "local_file", "name": "alice"})
        bob["serial"] += 1
        bob["resources"].append({"type": "local_file", "name": "bob"})
        self.client.put(alice)
        with self.assertRaises(main.HTTPStatusError) as caught:
            self.client.put(bob)
        self.assertEqual(caught.exception.status, 409)
        names = [r["name"] for r in self.client.get()["resources"]]
        self.assertEqual(names, ["alice"])


class AuthTests(ServerCase):
    credentials = ("tofu", "s3cret")

    def test_basic_auth_401_without_403_wrong_200_right_and_healthz_open(self):
        status, headers, _ = self.raw("GET", "/states/demo")
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])
        wrong = base64.b64encode(b"tofu:guess").decode()
        self.assertEqual(self.raw("GET", "/states/demo", headers={"Authorization": f"Basic {wrong}"})[0], 403)
        self.client.put_bytes(state_bytes(1))
        self.assertEqual(self.client.get()["serial"], 1)
        self.assertEqual(self.raw("GET", "/healthz")[0], 200)
        self.assertEqual(self.store.events("demo")[-1]["by"], "tofu")


class RequireLockTests(ServerCase):
    require_lock = True

    def test_require_lock_refuses_unlocked_writes_with_428(self):
        with self.assertRaises(main.HTTPStatusError) as caught:
            self.client.put_bytes(state_bytes(1))
        self.assertEqual(caught.exception.status, 428)
        state = main.locked_apply(self.client, lambda s: None, who="ci")
        self.assertEqual(state["serial"], 1)


class PureTests(unittest.TestCase):
    def test_check_order_rules(self):
        current = {"serial": 4, "lineage": "L"}
        self.assertEqual(main.check_order(None, None, {"serial": 1, "lineage": "X"}, "m"), "new")
        self.assertEqual(main.check_order(current, "m4", {"serial": 5, "lineage": "L"}, "m5"), "new")
        self.assertEqual(main.check_order(current, "m4", {"serial": 4, "lineage": "L"}, "m4"), "same")
        for incoming in ({"serial": 3, "lineage": "L"}, {"serial": 4, "lineage": "L"}, {"serial": 5, "lineage": "Z"}):
            with self.assertRaises(main.StoreError) as caught:
                main.check_order(current, "m4", incoming, "other")
            self.assertEqual(caught.exception.status, 409)

    def test_check_lock_rules(self):
        holder = {"ID": "a", "Who": "alice"}
        main.check_lock(holder, "a", False)
        main.check_lock(None, None, False)
        cases = [(holder, None, False, 423), (holder, "b", False, 409), (None, "a", False, 409), (None, None, True, 428)]
        for lock, lock_id, require, status in cases:
            with self.assertRaises(main.StoreError) as caught:
                main.check_lock(lock, lock_id, require)
            self.assertEqual(caught.exception.status, status)


class CliTests(ServerCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_lock_status_unlock_round_trip(self):
        code, lock_id, _ = self.run_cli("lock", self.address, "--who", "smoke-holder")
        self.assertEqual(code, 0)
        lock_id = lock_id.strip()
        code, _, err = self.run_cli("lock", self.address, "--who", "second")
        self.assertEqual(code, 1)
        self.assertIn(lock_id, err)
        self.assertIn("smoke-holder", err)
        code, out, _ = self.run_cli("status", self.address, "--json")
        self.assertEqual(json.loads(out)["lock"]["ID"], lock_id)
        self.assertEqual(self.run_cli("unlock", self.address, "--id", "wrong")[0], 1)
        self.assertEqual(self.run_cli("unlock", self.address, "--id", lock_id)[0], 0)
        code, out, _ = self.run_cli("status", self.address)
        self.assertIn("lock      free", out)

    def test_push_pull_history_and_exit_codes(self):
        path = os.path.join(self.tmp.name, "s.tfstate")
        with open(path, "wb") as handle:
            handle.write(state_bytes(2))
        self.assertEqual(self.run_cli("push", self.address, path)[0], 0)
        with open(path, "wb") as handle:
            handle.write(state_bytes(1, content="old"))
        code, _, err = self.run_cli("push", self.address, path)
        self.assertEqual(code, 1)
        self.assertIn("409", err)
        code, out, _ = self.run_cli("pull", self.address)
        self.assertEqual(json.loads(out)["serial"], 2)
        code, out, _ = self.run_cli("history", self.address)
        self.assertIn("VERSION", out)
        self.assertEqual(self.run_cli("push", self.address, os.path.join(self.tmp.name, "missing"))[0], 2)
        self.assertEqual(self.run_cli("lock")[0], 2)

    def test_admin_force_unlock_removes_a_lock_without_its_id(self):
        self.client.lock(who="crashed-runner")
        code, out, _ = self.run_cli("force-unlock", "--data", self.store.root, "demo")
        self.assertEqual(code, 0)
        self.assertIn("crashed-runner", out)
        self.assertIsNone(self.client.lock_status())
        self.assertEqual(self.store.events("demo")[-1]["event"], "force-unlock")


if __name__ == "__main__":
    unittest.main()
