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
            {"/tmp/cg/demo/memory.max": str(100 * 1024 ** 2), "/tmp/cg/demo/memory.swap.max": "0", "/tmp/cg/demo/cgroup.procs": "4242"},
        )

    def test_write_cgroup_skips_optional_files_that_the_kernel_does_not_expose(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            group = os.path.join(base, "demo")
            os.makedirs(group)
            with open(os.path.join(group, "memory.max"), "w") as handle:
                handle.write("max")
            with open(os.path.join(group, "cgroup.procs"), "w") as handle:
                handle.write("")
            spec = main.ContainerSpec(command=["/bin/sh"], name="demo", memory="50M", cgroup_base=base)
            main.write_cgroup(main.cgroup_files(spec, 7))
            with open(os.path.join(group, "memory.max")) as handle:
                self.assertEqual(handle.read(), str(50 * 1024 ** 2))
            with open(os.path.join(group, "cgroup.procs")) as handle:
                self.assertEqual(handle.read(), "7")
            self.assertFalse(os.path.exists(os.path.join(group, "memory.swap.max")), "optional file is not created when absent")

    def test_child_waits_until_parent_releases_it(self):
        ready_read, ready_write = os.pipe()
        main.release_child(ready_write)
        self.assertTrue(main.wait_for_parent(ready_read))

    def test_child_does_not_start_when_parent_fails_before_release(self):
        ready_read, ready_write = os.pipe()
        os.close(ready_write)
        self.assertFalse(main.wait_for_parent(ready_read))

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
