"""Tests for checks.py and paas_kit.py with fake runners, temp dirs and fixtures.
No Docker, no cluster and no network: every tool call goes through kit.RUNNER."""
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import checks  # noqa: E402
import paas_kit as kit  # noqa: E402

CAPSTONE = os.path.join(HERE, "..", "..")
NODE = "tinypaas-control-plane"
SHA = "3f9c2a1b7d4e5f60718293a4b5c6d7e8f9a0b1c2"
DIGEST = "sha256:" + "ab" * 32


class FakeRunner:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def __call__(self, args, timeout=60, **kwargs):
        self.calls.append(list(args))
        joined = " ".join(args)
        for needle, proc in self.responses:
            if needle in joined:
                return proc
        return kit.Proc(0, "", "")


def ok(out=""):
    return kit.Proc(0, out, "")


def bad(err, code=1):
    return kit.Proc(code, "", err)


def write_tinypaas(project, body):
    path = os.path.join(project, "bin", "tinypaas")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!" + sys.executable + "\n" + textwrap.dedent(body))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        kit.reset_target()
        self.saved = (kit.RUNNER, kit.SLEEP, kit.CLOCK)

    def tearDown(self):
        kit.RUNNER, kit.SLEEP, kit.CLOCK = self.saved
        kit.reset_target()
        shutil.rmtree(self.tmp, ignore_errors=True)


class ManifestCaseTests(Base):
    def case(self, valid, paths):
        return {"id": "c", "valid": valid, "paths": paths}

    def test_matching_verdict_and_paths_has_no_problems(self):
        data = {"valid": False, "errors": [{"path": "$.port", "message": "x"}, {"path": "$.health", "message": "y"}]}
        self.assertEqual(kit.compare_case(self.case(False, ["$.health", "$.port"]), 1, data), [])

    def test_missing_path_is_reported_with_both_sets(self):
        data = {"valid": False, "errors": [{"path": "$.port", "message": "x"}]}
        problems = kit.compare_case(self.case(False, ["$.health", "$.port"]), 1, data)
        self.assertEqual(len(problems), 1)
        self.assertIn("expected paths ['$.health', '$.port'], got ['$.port']", problems[0])

    def test_exit_code_must_follow_the_verdict(self):
        problems = kit.compare_case(self.case(True, []), 1, {"valid": True, "errors": []})
        self.assertEqual(problems, ["case c: exit 1, expected 0"])

    def test_malformed_output_explains_the_shape(self):
        problems = kit.compare_case(self.case(True, []), 0, {"ok": True})
        self.assertIn("valid (true/false) and errors", problems[0])

    def test_fixture_cases_are_well_formed(self):
        cases = kit.manifest_cases()
        self.assertGreaterEqual(len(cases), 10)
        self.assertEqual(len({c["id"] for c in cases}), len(cases))
        for c in cases:
            self.assertEqual(c["valid"], not c["paths"], c["id"])
            self.assertTrue(all(p == "$" or p.startswith("$.") for p in c["paths"]), c["id"])

    def test_cheating_free_validator_passes_and_a_lenient_one_fails(self):
        oracle = os.path.join(self.tmp, "oracle.json")
        with open(oracle, "w", encoding="utf-8") as f:
            json.dump({c["id"]: c for c in kit.manifest_cases()}, f)
        project = os.path.join(self.tmp, "good")
        write_tinypaas(project, f"""
            import json, os, sys
            cases = json.load(open({oracle!r}))
            case = cases[os.path.basename(sys.argv[2])]
            print(json.dumps({{"valid": case["valid"], "errors": [{{"path": p, "message": "m"}} for p in case["paths"]]}}))
            sys.exit(0 if case["valid"] else 1)
        """)
        passed, detail = checks.check_app_manifest(project)
        self.assertTrue(passed, detail)
        lenient = os.path.join(self.tmp, "lenient")
        write_tinypaas(lenient, """
            import json
            print(json.dumps({"valid": True, "errors": []}))
        """)
        passed, detail = checks.check_app_manifest(lenient)
        self.assertFalse(passed)
        self.assertIn("expected valid=false", detail)

    def test_fixture_apps_are_valid_python_and_json(self):
        for root, _, files in os.walk(kit.APPS):
            for name in files:
                path = os.path.join(root, name)
                if name.endswith(".py"):
                    with open(path, encoding="utf-8") as f:
                        compile(f.read(), path, "exec")
                if name.endswith(".json"):
                    with open(path, encoding="utf-8") as f:
                        self.assertIsInstance(json.load(f), dict, path)

    def test_starter_fails_the_manifest_milestone_with_its_stub_message(self):
        passed, detail = checks.check_app_manifest(os.path.join(CAPSTONE, "starter"))
        self.assertFalse(passed)
        self.assertIn("not implemented: milestone app-manifest", detail)


class ProjectTests(Base):
    def test_missing_entry_point_names_the_file(self):
        passed, detail = checks.check_deploy(self.tmp)
        self.assertFalse(passed)
        self.assertIn("bin/tinypaas is missing", detail)

    def test_entry_point_must_be_executable(self):
        path = write_tinypaas(self.tmp, "print('hi')\n")
        os.chmod(path, 0o644)
        with self.assertRaises(kit.Fail) as ctx:
            kit.executable(self.tmp)
        self.assertIn("chmod +x bin/tinypaas", str(ctx.exception))

    def test_cluster_checks_fail_fast_without_kind(self):
        write_tinypaas(self.tmp, "print('hi')\n")
        with mock.patch.object(kit.shutil, "which", lambda tool: None if tool == "kind" else "/usr/bin/" + tool):
            passed, detail = checks.check_build_image(self.tmp)
        self.assertFalse(passed)
        self.assertIn("kind is not on PATH", detail)


class TargetTests(Base):
    def setUp(self):
        super().setUp()
        self.which = mock.patch.object(kit.shutil, "which", lambda tool: "/usr/bin/" + tool)
        self.which.start()
        self.addCleanup(self.which.stop)

    def test_refuses_a_context_that_is_not_kind(self):
        kit.RUNNER = FakeRunner([("current-context", ok("prod-eu-1\n"))])
        with mock.patch.dict(os.environ, {"CC_GRADER_CONTEXT": ""}):
            with self.assertRaises(kit.Fail) as ctx:
                kit.target()
        self.assertIn("refusing to grade against context prod-eu-1", str(ctx.exception))

    def test_kind_context_gives_cluster_name_and_nodes(self):
        kit.RUNNER = FakeRunner([("current-context", ok("kind-tinypaas\n")), ("get nodes --name tinypaas", ok(NODE + "\n"))])
        target = kit.target()
        self.assertEqual((target.context, target.cluster, target.nodes), ("kind-tinypaas", "tinypaas", [NODE]))

    def test_allowed_context_still_needs_the_kind_cluster_name(self):
        kit.RUNNER = FakeRunner([("current-context", ok("lab\n"))])
        with mock.patch.dict(os.environ, {"CC_GRADER_CONTEXT": "lab", "CC_GRADER_KIND_CLUSTER": ""}):
            with self.assertRaises(kit.Fail) as ctx:
                kit.target()
        self.assertIn("CC_GRADER_KIND_CLUSTER", str(ctx.exception))

    def test_unreachable_api_server_is_reported(self):
        kit.RUNNER = FakeRunner([("current-context", ok("kind-x\n")), ("get nodes", ok("x-control-plane\n")), ("/readyz", bad("connection refused"))])
        with self.assertRaises(kit.Fail) as ctx:
            kit.target()
        self.assertIn("does not answer /readyz", str(ctx.exception))


class VerifyBuildTests(Base):
    def session(self, runner):
        kit.RUNNER = runner
        session = mock.Mock()
        session.target = kit.Target("kind-tinypaas", "tinypaas", [NODE])
        session.node_images = lambda node: kit.Session.node_images(session, node)
        return session

    def images(self, *tags):
        return ok(json.dumps({"images": [{"id": "sha256:1", "repoTags": list(tags)}]}))

    def test_good_build_is_accepted(self):
        runner = FakeRunner([("image inspect", ok(DIGEST + "\n")), ("crictl images", self.images("docker.io/tinypaas/ccg-hello:3f9c2a1"))])
        ref = checks.verify_build(self.session(runner), "ccg-hello", SHA, {"image": "tinypaas/ccg-hello:3f9c2a1", "digest": DIGEST})
        self.assertEqual(ref, "tinypaas/ccg-hello:3f9c2a1")

    def test_tag_must_be_the_commit(self):
        runner = FakeRunner([("image inspect", ok(DIGEST))])
        with self.assertRaises(kit.Fail) as ctx:
            checks.verify_build(self.session(runner), "ccg-hello", SHA, {"image": "tinypaas/ccg-hello:latest", "digest": DIGEST})
        self.assertIn("not the short sha", str(ctx.exception))

    def test_digest_must_match_docker(self):
        runner = FakeRunner([("image inspect", ok("sha256:" + "cd" * 32))])
        with self.assertRaises(kit.Fail) as ctx:
            checks.verify_build(self.session(runner), "ccg-hello", SHA, {"image": "tinypaas/ccg-hello:3f9c2a1", "digest": DIGEST})
        self.assertIn("content digest", str(ctx.exception))

    def test_image_must_be_loaded_into_every_node(self):
        runner = FakeRunner([("image inspect", ok(DIGEST)), ("crictl images", self.images("docker.io/library/python:3.13-alpine"))])
        with self.assertRaises(kit.Fail) as ctx:
            checks.verify_build(self.session(runner), "ccg-hello", SHA, {"image": "tinypaas/ccg-hello:3f9c2a1", "digest": DIGEST})
        self.assertIn("kind load docker-image", str(ctx.exception))


class HelperTests(Base):
    def test_timestamps_order_by_value_not_by_string(self):
        short, longer = "2026-11-13T10:00:00.1Z", "2026-11-13T10:00:00.12Z"
        self.assertGreater(short, longer)
        self.assertLess(kit.parse_timestamp(short), kit.parse_timestamp(longer))
        self.assertEqual(kit.parse_timestamp("2026-11-13T12:00:00+02:00"), kit.parse_timestamp("2026-11-13T10:00:00Z"))

    def test_quantities_compare_by_value(self):
        self.assertEqual((kit.cpu_millis("100m"), kit.cpu_millis("0.1"), kit.cpu_millis("1")), (100, 100, 1000))
        self.assertEqual(kit.memory_bytes("64Mi"), 64 * 1024 * 1024)
        self.assertIsNone(kit.memory_bytes("64MB"))

    def test_releases_must_be_numbered_in_order_with_rfc3339_times(self):
        release = {"image": "i", "config_hash": "h", "status": "succeeded", "time": "2026-11-13T10:00:00Z"}
        self.assertEqual(len(kit.releases_of({"current": 2, "releases": [dict(release, number=1), dict(release, number=2)]})), 2)
        with self.assertRaises(kit.Fail):
            kit.releases_of({"current": 3, "releases": [dict(release, number=1), dict(release, number=3)]})
        with self.assertRaises(kit.Fail):
            kit.releases_of({"current": 1, "releases": [dict(release, number=1, time="yesterday")]})

    def test_poll_stops_at_the_deadline_without_real_sleeping(self):
        now = [0.0]
        kit.CLOCK = lambda: now[0]
        kit.SLEEP = lambda seconds: now.__setitem__(0, now[0] + seconds)
        ok_, value = kit.poll(lambda: (False, now[0]), timeout=5, interval=1)
        self.assertFalse(ok_)
        self.assertEqual(value, 5.0)
        self.assertEqual(kit.holds(lambda: (now[0] < 7, now[0]), seconds=10), (False, 7.0))

    def test_fetch_via_node_splits_body_and_status(self):
        kit.RUNNER = FakeRunner([("curl", ok("hello\nworld\n200"))])
        self.assertEqual(kit.fetch_via_node(NODE, "http://172.18.0.2:30080/", 5), (200, "hello\nworld"))

    def test_refused_port_means_the_host_is_reachable_directly(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.assertEqual(kit.route_for("127.0.0.1", port), "direct")

    def test_cleanup_removes_only_this_apps_images(self):
        runner = FakeRunner([
            ("image ls", ok("tinypaas/ccg-hello:3f9c2a1\ntinypaas/ccg-hello-two:aaaaaaa\nmyorg/app:1\n")),
            ("crictl images", ok(json.dumps({"images": [{"repoTags": ["docker.io/tinypaas/ccg-hello:3f9c2a1", "docker.io/library/python:3.13-alpine"]}]}))),
        ])
        kit.RUNNER = runner
        session = mock.Mock()
        session.target = kit.Target("kind-tinypaas", "tinypaas", [NODE])
        session.node_images = lambda node: kit.Session.node_images(session, node)
        kit.Session.remove_images(session, "ccg-hello")
        removed = [c for c in runner.calls if "rm" in c or "rmi" in c]
        self.assertEqual(removed, [["docker", "image", "rm", "-f", "tinypaas/ccg-hello:3f9c2a1"],
                                   ["docker", "exec", NODE, "crictl", "rmi", "docker.io/tinypaas/ccg-hello:3f9c2a1"]])


if __name__ == "__main__":
    unittest.main()
