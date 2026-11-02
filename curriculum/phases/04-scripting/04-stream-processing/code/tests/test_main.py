import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


class MainTests(unittest.TestCase):
    def test_main_returns_zero(self):
        self.assertEqual(main.main([]), 0)

    def test_todo_1(self):
        self.fail("write me")

    def test_todo_2(self):
        self.fail("write me")

    def test_todo_3(self):
        self.fail("write me")

    def test_todo_4(self):
        self.fail("write me")


if __name__ == "__main__":
    unittest.main()
