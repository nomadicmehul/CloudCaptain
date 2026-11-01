import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


def fake_runner(outputs):
    def run(cmd):
        binary = cmd[0]
        if binary not in outputs:
            return 127, f"{binary}: not found"
        code, text = outputs[binary]
        return code, text
    return run


def fake_which(available):
    return lambda name: f"/usr/bin/{name}" if name in available else None


class VersionTests(unittest.TestCase):
    def test_parse_version_handles_real_tool_output(self):
        self.assertEqual(main.parse_version("Docker version 27.3.1, build ce12230"), (27, 3, 1))
        self.assertEqual(main.parse_version("kind v0.33.0 go1.24.1 darwin/arm64"), (0, 33, 0))
        self.assertEqual(main.parse_version("clientVersion:\n  gitVersion: v1.31.2\n"), (1, 31, 2))
        self.assertEqual(main.parse_version("OpenTofu v1.13.2\non darwin_arm64"), (1, 13, 2))
        self.assertIsNone(main.parse_version("no digits here"))

    def test_version_at_least_pads_missing_parts(self):
        self.assertTrue(main.version_at_least((1, 31), "1.30"))
        self.assertTrue(main.version_at_least((1, 30, 0), "1.30"))
        self.assertFalse(main.version_at_least((1, 29, 9), "1.30"))
        self.assertTrue(main.version_at_least((3, 9, 6), "3.9"))


class ProbeTests(unittest.TestCase):
    def tool(self, name):
        return next(t for t in main.TOOLS if t.name == name)

    def test_probe_reports_ok_old_missing_and_broken(self):
        runner = fake_runner({"docker": (0, "Docker version 27.3.1"), "git": (0, "git version 2.20.1"), "kind": (0, "weird output")})
        which = fake_which({"docker", "git", "kind"})
        self.assertEqual(main.probe(self.tool("docker"), runner, which).status, "ok")
        self.assertEqual(main.probe(self.tool("git"), runner, which).status, "old")
        self.assertEqual(main.probe(self.tool("kubectl"), runner, which).status, "missing")
        self.assertEqual(main.probe(self.tool("kind"), runner, which).status, "broken")

    def test_probe_accepts_alternative_binaries_with_their_own_minimums(self):
        runner = fake_runner({"tofu": (0, "OpenTofu v1.13.2"), "podman": (0, "podman version 5.4.0")})
        which = fake_which({"tofu", "podman"})
        iac = main.probe(self.tool("iac"), runner, which)
        self.assertEqual((iac.status, iac.binary, iac.found), ("ok", "tofu", "1.13.2"))
        docker = main.probe(self.tool("docker"), runner, which)
        self.assertEqual((docker.status, docker.binary), ("ok", "podman"))
        self.assertEqual(self.tool("docker").minimum_for("podman"), "4.0")
        self.assertEqual(self.tool("docker").minimum_for("docker"), "24.0")
        old_podman = main.probe(self.tool("docker"), fake_runner({"podman": (0, "podman version 3.4.4")}), fake_which({"podman"}))
        self.assertEqual(old_podman.status, "old")


class PlanAndReportTests(unittest.TestCase):
    def results(self):
        runner = fake_runner({"git": (0, "git version 2.45.0"), "python3": (0, "Python 3.12.3"), "docker": (0, "Docker version 20.10.0")})
        which = fake_which({"git", "python3", "docker"})
        return [main.probe(tool, runner, which) for tool in main.TOOLS]

    def test_plan_install_only_lists_missing_or_old_tools_for_the_package_manager(self):
        commands = main.plan_install(self.results(), "brew")
        self.assertIn("brew install --cask docker", commands)
        self.assertIn("brew install kind", commands)
        self.assertNotIn("brew install git", commands)
        apt = main.plan_install(self.results(), "apt")
        self.assertIn("curl -fsSL https://get.docker.com | sh", apt)

    def test_report_markdown_has_a_status_row_per_tool_and_the_plan(self):
        results = self.results()
        text = main.report_markdown(results, "macos", "brew", main.plan_install(results, "brew"), ["cgroup v2: yes"])
        self.assertIn("| git | clone the curriculum and keep your artifacts | 2.30 | git 2.45.0 | ✅ ok |", text)
        self.assertIn("⚠️ old", text)
        self.assertIn("❌ missing", text)
        self.assertIn("## Install plan", text)
        self.assertIn("brew install kind", text)

    def test_main_json_reports_readiness_and_exit_code(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main.main(["check", "--json"], runner=fake_runner({}))
        data = json.loads(out.getvalue())
        self.assertFalse(data["ready"])
        self.assertEqual(code, 1)
        self.assertEqual({t["name"] for t in data["tools"]}, {t.name for t in main.TOOLS})


if __name__ == "__main__":
    unittest.main()
