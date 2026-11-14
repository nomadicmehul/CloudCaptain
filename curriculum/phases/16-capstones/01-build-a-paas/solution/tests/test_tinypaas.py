import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tinypaas import hooks, images, logs, manifest, releases, render  # noqa: E402
from tinypaas.cluster import Cluster  # noqa: E402
from tinypaas.runner import PaasError, Result  # noqa: E402

GOOD = {"name": "web", "web": "python3 app.py", "port": 8080, "health": "/healthz", "env": {"GREETING": "hi"}}


def write_app(folder, data, extra=None):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "app.json"), "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))
    for rel, content in (extra or {}).items():
        path = os.path.join(folder, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    return folder


def paths(report):
    return sorted(e["path"] for e in report["errors"])


class FakeRun:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, args, input=None, timeout=120, env=None, cwd=None):
        self.calls.append((list(args), input))
        for needle, result in self.answers:
            if needle in " ".join(args):
                return result
        return Result(0, "", "")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class ManifestTests(Base):
    def test_good_manifest_is_valid_and_normalized_with_defaults(self):
        report = manifest.validate(write_app(self.tmp, GOOD))
        self.assertTrue(report["valid"])
        self.assertEqual(report["manifest"]["resources"], {"cpu": "100m", "memory": "128Mi"})
        self.assertEqual(report["manifest"]["replicas"], 1)

    def test_every_error_has_its_own_json_path(self):
        bad = {"name": "Web_App", "port": "80", "health": "healthz", "env": {"lower": "x", "N": 1}, "colour": "blue"}
        report = manifest.validate(write_app(self.tmp, bad))
        self.assertEqual(paths(report), ["$.colour", "$.env.N", "$.env.lower", "$.health", "$.name", "$.port", "$.web"])

    def test_dockerfile_replaces_the_web_command(self):
        data = {k: v for k, v in GOOD.items() if k != "web"}
        self.assertFalse(manifest.validate(write_app(self.tmp, data))["valid"])
        report = manifest.validate(write_app(self.tmp, data, {"Dockerfile": "FROM scratch\n"}))
        self.assertTrue(report["valid"])
        self.assertTrue(report["manifest"]["dockerfile"])

    def test_unparseable_json_is_reported_at_the_root(self):
        self.assertEqual(paths(manifest.validate(write_app(self.tmp, "{"))), ["$"])
        self.assertEqual(paths(manifest.validate(os.path.join(self.tmp, "nowhere"))), ["$"])


class ImageTests(unittest.TestCase):
    def normalized(self, **changes):
        data = dict(GOOD, resources=dict(manifest.DEFAULT_RESOURCES), replicas=1, static=None, dockerfile=False)
        data.update(changes)
        return data

    def test_python_dockerfile_execs_the_web_command(self):
        text = images.generated_dockerfile(self.normalized(), "python:3.13-alpine")
        self.assertIn("FROM python:3.13-alpine", text)
        self.assertIn('CMD ["sh", "-c", "exec python3 app.py"]', text)
        self.assertNotIn("pip install", text)
        self.assertIn("pip install", images.generated_dockerfile(self.normalized(), "python:3.13-alpine", has_requirements=True))

    def test_static_dockerfile_serves_the_directory(self):
        text = images.generated_dockerfile(self.normalized(web=None, static="public/"), "python:3.13-alpine")
        self.assertIn("COPY public/ /srv/", text)
        self.assertIn("http.server", text)

    def test_image_ref_uses_the_short_sha(self):
        self.assertEqual(images.image_ref("web", "3f9c2a1b7d4e"), "tinypaas/web:3f9c2a1")

    def test_build_refuses_a_folder_without_commits(self):
        run = FakeRun([("is-inside-work-tree", Result(128, "", "not a git repository"))])
        with self.assertRaises(PaasError) as ctx:
            images.build("/tmp/x", run, "tinypaas")
        self.assertIn("git init", str(ctx.exception))


class RenderTests(unittest.TestCase):
    def release(self, **changes):
        data = {"number": 3, "image": "tinypaas/web:3f9c2a1", "config": {"GREETING": "hi"}, "config_hash": "sha256:x",
                "manifest": dict(GOOD, resources={"cpu": "100m", "memory": "64Mi"}, replicas=1, static=None, dockerfile=False)}
        data.update(changes)
        return data

    def test_deployment_carries_probes_limits_env_and_release(self):
        deployment = render.deployment("web", "tinypaas-web", self.release(), replicas=2)
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/healthz")
        self.assertEqual(container["resources"]["limits"], {"cpu": "100m", "memory": "64Mi"})
        self.assertEqual(container["env"], [{"name": "GREETING", "value": "hi"}, {"name": "PORT", "value": "8080"}])
        self.assertEqual(deployment["spec"]["template"]["metadata"]["annotations"][render.RELEASE_ANNOTATION], "3")
        self.assertEqual(deployment["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"], 0)

    def test_service_is_a_node_port_on_the_named_port(self):
        service = render.service("web", "tinypaas-web")
        self.assertEqual(service["spec"]["type"], "NodePort")
        self.assertEqual(service["spec"]["ports"][0]["targetPort"], "http")


class ReleaseTests(Base):
    def state_with(self, *statuses):
        store = releases.Store(self.tmp, clock=lambda: 1767225600)
        state = store.load("web")
        for i, status in enumerate(statuses):
            build = {"image": f"tinypaas/web:{i}", "manifest": {"env": {}}}
            store.add_release(state, build, {"N": str(i)}, f"Deploy {i}")["status"] = status
            if status == releases.SUCCEEDED:
                state["current"] = i + 1
        store.save(state)
        return store, store.load("web")

    def test_config_hash_ignores_key_order(self):
        self.assertEqual(releases.config_hash({"A": "1", "B": "2"}), releases.config_hash({"B": "2", "A": "1"}))
        self.assertNotEqual(releases.config_hash({"A": "1"}), releases.config_hash({"A": "2"}))

    def test_releases_are_numbered_and_timestamped(self):
        _, state = self.state_with(releases.SUCCEEDED, releases.SUCCEEDED)
        self.assertEqual([r["number"] for r in state["releases"]], [1, 2])
        self.assertEqual(state["releases"][0]["time"], "2026-01-01T00:00:00Z")

    def test_rollback_skips_failed_releases(self):
        _, state = self.state_with(releases.SUCCEEDED, releases.SUCCEEDED, releases.FAILED, releases.SUCCEEDED)
        self.assertEqual(releases.rollback_target(state, None)["number"], 2)
        self.assertEqual(releases.rollback_target(state, 1)["number"], 1)
        with self.assertRaises(PaasError):
            releases.rollback_target(state, 3)


class LogTests(unittest.TestCase):
    def test_merge_orders_by_parsed_time_not_by_string(self):
        merged = logs.merge({
            "web-a": "2026-11-13T10:00:00.1Z first\n2026-11-13T10:00:02Z third\n",
            "web-b": "2026-11-13T10:00:00.12Z second\nno timestamp here\n",
        })
        self.assertEqual([(e["pod"], e["line"]) for e in merged], [("web-a", "first"), ("web-b", "second"), ("web-a", "third")])

    def test_since_takes_a_duration(self):
        self.assertTrue(logs.valid_since("5m"))
        self.assertFalse(logs.valid_since("5 minutes"))


class ClusterAndHookTests(Base):
    def test_kind_cluster_name_comes_from_the_context(self):
        cluster = Cluster(FakeRun([("current-context", Result(0, "kind-dev\n", ""))]))
        self.assertEqual((cluster.kind_name, cluster.namespace("web")), ("dev", "tinypaas-web"))
        with self.assertRaises(PaasError):
            _ = Cluster(FakeRun([]), context="prod").kind_name

    def test_post_receive_deploys_only_main(self):
        script = hooks.post_receive("/usr/bin/python3", "/opt/tinypaas", "web", {"TINYPAAS_HOME": "/data dir"})
        self.assertIn('"$REF" != "refs/heads/main"', script)
        self.assertIn("export TINYPAAS_HOME='/data dir'", script)
        self.assertIn("deploy \"$WORK/src\" --app web", script)
        if shutil.which("bash"):
            self.assertEqual(subprocess.run(["bash", "-n"], input=script, text=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
