import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from copilot.config import DEFAULT_MODEL, Refused, load_config
from copilot.kube import Kube


class ConfigTests(unittest.TestCase):
    def test_empty_allow_list_refuses_every_namespace(self):
        with self.assertRaises(Refused):
            load_config({}).require("default")

    def test_patterns_allow_matching_namespaces(self):
        config = load_config({"COPILOT_ALLOWED_NAMESPACES": "shop, cc-grade-*"})
        self.assertEqual(config.require("cc-grade-copilot-1"), "cc-grade-copilot-1")
        self.assertFalse(config.allows("kube-system"))

    def test_model_defaults(self):
        config = load_config({})
        self.assertEqual((config.model, config.model_url_set), (DEFAULT_MODEL, False))

    def test_kube_pins_the_context(self):
        self.assertEqual(Kube("kind-lab").argv("get", "pods"), ["kubectl", "--context", "kind-lab", "get", "pods"])


if __name__ == "__main__":
    unittest.main()
