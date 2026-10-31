import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


class ParseMemoryTests(unittest.TestCase):
    def test_parse_memory_understands_units(self):
        self.assertEqual(main.parse_memory("100M"), 100 * 1024 ** 2)
        self.assertEqual(main.parse_memory("1G"), 1024 ** 3)
        self.assertEqual(main.parse_memory("512k"), 512 * 1024)
        self.assertEqual(main.parse_memory("42"), 42)
        self.assertEqual(main.parse_memory("100MB"), main.parse_memory("100M"))

    def test_parse_memory_rejects_garbage(self):
        for bad in ("lots", "10T", "", "M", "1.5G"):
            with self.assertRaises(ValueError, msg=bad):
                main.parse_memory(bad)


class NamespaceFlagTests(unittest.TestCase):
    def test_unshare_flags_combines_known_namespaces(self):
        self.assertEqual(main.unshare_flags(("uts", "pid")), main.CLONE_NEWUTS | main.CLONE_NEWPID)
        self.assertTrue(main.unshare_flags(main.DEFAULT_NAMESPACES) & main.CLONE_NEWNS)
        self.assertEqual(main.unshare_flags(()), 0)

    def test_unshare_flags_rejects_unknown_namespace(self):
        with self.assertRaises(ValueError):
            main.unshare_flags(("uts", "time"))


class CgroupTests(unittest.TestCase):
    def test_cgroup_files_write_limit_and_pid(self):
        spec = main.ContainerSpec(command=["/bin/sh"], name="demo", memory="100M", cgroup_base="/tmp/cg")
        self.assertEqual(
            main.cgroup_files(spec, 4242),
            {"/tmp/cg/demo/memory.max": str(100 * 1024 ** 2), "/tmp/cg/demo/cgroup.procs": "4242"},
        )

    def test_cgroup_files_empty_without_memory_limit(self):
        spec = main.ContainerSpec(command=["/bin/sh"])
        self.assertEqual(main.cgroup_files(spec, 1), {})


class CliTests(unittest.TestCase):
    def test_parse_args_defaults(self):
        spec = main.parse_args([])
        self.assertEqual(spec.command, ["/bin/sh"])
        self.assertEqual(spec.hostname, "box")
        self.assertEqual(spec.namespaces, main.DEFAULT_NAMESPACES)
        self.assertIsNone(spec.memory)
        self.assertFalse(spec.dry_run)

    def test_parse_args_reads_remainder_after_double_dash(self):
        spec = main.parse_args(["--hostname", "web", "--ns", "uts,net", "--memory", "64M", "--", "/bin/sh", "-c", "echo hi"])
        self.assertEqual(spec.command, ["/bin/sh", "-c", "echo hi"])
        self.assertEqual(spec.hostname, "web")
        self.assertEqual(spec.namespaces, ("uts", "net"))
        self.assertEqual(spec.memory, "64M")

    def test_parse_args_rejects_bad_namespace_and_memory(self):
        with self.assertRaises(ValueError):
            main.parse_args(["--ns", "uts,bogus"])
        with self.assertRaises(ValueError):
            main.parse_args(["--memory", "plenty"])


class ExplainTests(unittest.TestCase):
    def test_explain_mentions_each_isolation_layer(self):
        spec = main.ContainerSpec(command=["/bin/sh"], rootfs="/srv/alpine", memory="100M", namespaces=("uts", "pid", "mnt", "net"))
        text = "\n".join(main.explain(spec))
        for needle in ("uts namespace", "pid namespace", "mnt namespace", "net namespace", "chroot", "memory.max"):
            self.assertIn(needle, text)

    def test_main_explain_prints_and_returns_zero(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main.main(["--explain", "--memory", "100M", "--", "sleep", "1"])
        self.assertEqual(code, 0)
        self.assertIn("process: sleep 1", out.getvalue())
        self.assertIn("104857600 bytes", out.getvalue())


if __name__ == "__main__":
    unittest.main()
