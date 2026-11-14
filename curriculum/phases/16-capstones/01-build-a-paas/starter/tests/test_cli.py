import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tinypaas import cli  # noqa: E402
from tinypaas.settings import Settings  # noqa: E402


class CliTests(unittest.TestCase):
    def test_every_command_parses(self):
        p = cli.parser()
        self.assertEqual(p.parse_args(["deploy", "app", "--timeout", "40"]).timeout, 40)
        self.assertEqual(p.parse_args(["config", "set", "web", "A=1", "B=2"]).pairs, ["A=1", "B=2"])
        self.assertEqual(p.parse_args(["rollback", "web", "--to", "3"]).to, 3)
        self.assertEqual(p.parse_args(["logs", "web"]).since, "5m")

    def test_settings_follow_the_grader_variables(self):
        s = Settings.load(None, {"TINYPAAS_HOME": "/tmp/h", "TINYPAAS_NAMESPACE_PREFIX": "cc-grade-paas", "TINYPAAS_KIND_CLUSTER": "dev"})
        self.assertEqual((s.state_dir, s.namespace("web"), s.kind_cluster_name()), ("/tmp/h/.tinypaas", "cc-grade-paas-web", "dev"))


if __name__ == "__main__":
    unittest.main()
