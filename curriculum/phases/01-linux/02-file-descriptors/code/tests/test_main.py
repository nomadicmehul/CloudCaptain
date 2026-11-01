import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

LIMITS = """Limit                     Soft Limit           Hard Limit           Units
Max cpu time              unlimited            unlimited            seconds
Max open files            {soft}                 {hard}               files
Max processes             4096                 4096                 processes
"""


def write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def make_proc(root, pid, comm="app", fds=None, soft="1024", hard="524288", flags="02000002"):
    base = os.path.join(root, str(pid))
    os.makedirs(os.path.join(base, "fd"))
    os.makedirs(os.path.join(base, "fdinfo"))
    write(os.path.join(base, "comm"), comm + "\n")
    write(os.path.join(base, "limits"), LIMITS.format(soft=soft, hard=hard))
    for fd, target in (fds or {}).items():
        os.symlink(target, os.path.join(base, "fd", str(fd)))
        write(os.path.join(base, "fdinfo", str(fd)), f"pos:\t0\nflags:\t{flags}\nmnt_id:\t28\n")
    return base


class FakeProc(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fdwatch-proc-")
        self.files = tempfile.mkdtemp(prefix="fdwatch-files-")
        self.log = os.path.join(self.files, "app.log")
        write(self.log, "x" * 4096)
        self.addCleanup(shutil.rmtree, self.root)
        self.addCleanup(shutil.rmtree, self.files)

    def typical(self, pid=4242, extra=None):
        fds = {
            0: "/dev/pts/3",
            1: "/dev/pts/3",
            2: "/dev/null",
            3: "socket:[31337]",
            4: "pipe:[555]",
            5: "anon_inode:[eventpoll]",
            6: self.log,
            7: self.log,
            8: self.files,
            9: os.path.join(self.files, "gone.log") + " (deleted)",
        }
        fds.update(extra or {})
        make_proc(self.root, pid, "worker", fds)
        return main.snapshot(pid, self.root, now=lambda: 1000.0)


class ClassifyTests(unittest.TestCase):
    def test_classify_pseudo_targets_devices_files_and_deleted_suffix(self):
        self.assertEqual(main.classify("socket:[12345]"), ("socket", False))
        self.assertEqual(main.classify("pipe:[777]"), ("pipe", False))
        self.assertEqual(main.classify("anon_inode:[eventpoll]"), ("epoll", False))
        self.assertEqual(main.classify("anon_inode:[timerfd]"), ("timerfd", False))
        self.assertEqual(main.classify("anon_inode:inotify"), ("inotify", False))
        self.assertEqual(main.classify("anon_inode:[something-new]"), ("anon_inode", False))
        self.assertEqual(main.classify("/dev/pts/3"), ("device", False))
        self.assertEqual(main.classify("/etc/hosts"), ("file", False))
        self.assertEqual(main.classify("/var/log/app.log (deleted)"), ("file", True))

    def test_parse_limits_reads_soft_hard_and_unlimited(self):
        self.assertEqual(main.parse_limits(LIMITS.format(soft="1024", hard="524288")), (1024, 524288))
        self.assertEqual(main.parse_limits(LIMITS.format(soft="unlimited", hard="unlimited")), (None, None))
        self.assertEqual(main.parse_limits("Max processes 1 2 processes\n"), (None, None))

    def test_fdinfo_and_flag_words(self):
        info = main.parse_fdinfo("pos:\t8192\nflags:\t02100001\nmnt_id:\t28\n")
        self.assertEqual(info, {"pos": 8192, "flags": 0o2100001})
        self.assertEqual(main.describe_flags(0o2002001), "w cloexec append")
        self.assertEqual(main.describe_flags(0o2), "rw")
        self.assertEqual(main.describe_flags(0o4000), "r nonblock")


class SnapshotTests(FakeProc):
    def test_snapshot_reads_the_fake_proc_tree(self):
        snap = self.typical()
        self.assertEqual((snap.pid, snap.comm, snap.count), (4242, "worker", 10))
        self.assertEqual((snap.soft_limit, snap.hard_limit), (1024, 524288))
        by_fd = {d.fd: d for d in snap.descriptors}
        self.assertEqual(by_fd[3].kind, "socket")
        self.assertEqual(by_fd[5].kind, "epoll")
        self.assertEqual((by_fd[6].kind, by_fd[6].size), ("file", 4096))
        self.assertEqual(by_fd[8].kind, "directory")
        self.assertTrue(by_fd[9].deleted)
        self.assertEqual(by_fd[9].target, os.path.join(self.files, "gone.log"))
        self.assertEqual(by_fd[6].flags, 0o2000002)
        self.assertAlmostEqual(snap.usage, 10 / 1024)

    def test_kinds_top_targets_and_deleted_summaries(self):
        snap = self.typical()
        self.assertEqual(main.kinds(snap), {"device": 3, "file": 2, "socket": 1, "pipe": 1, "epoll": 1, "directory": 1, "file (deleted)": 1})
        self.assertEqual(main.top_targets(snap, 1), [("/dev/pts/3", 2)])
        self.assertEqual([d.fd for d in main.deleted_open(snap)], [9])
        self.assertEqual(main.without_cloexec(snap), [])

    def test_missing_process_raises(self):
        with self.assertRaises(FileNotFoundError):
            main.snapshot(99999, self.root)


class DiffTests(FakeProc):
    def test_diff_measures_growth_rate_and_time_to_limit(self):
        before = self.typical(pid=1)
        extra = {10 + i: os.path.join(self.files, f"leak{i}.tmp") for i in range(60)}
        after = self.typical(pid=2, extra=extra)
        after.taken_at = before.taken_at + 30
        delta = main.diff(before, after)
        self.assertEqual((delta.before, delta.after, delta.verdict), (10, 70, "growing"))
        self.assertEqual(delta.by_kind, {"file": 60})
        self.assertAlmostEqual(delta.rate_per_minute, 120.0)
        self.assertAlmostEqual(delta.seconds_to_limit, (1024 - 70) / 2.0)
        self.assertEqual(len(delta.new_targets), 5)
        text = main.render_delta(delta)
        self.assertIn("10 -> 70 (+60) in 30s: growing", text)
        self.assertIn("rate 120.0/min", text)
        self.assertIn("soft limit reached in about 7m 57s", text)

    def test_stable_snapshots_have_no_rate_warning(self):
        a = self.typical(pid=1)
        b = self.typical(pid=2)
        b.taken_at += 5
        delta = main.diff(a, b)
        self.assertEqual((delta.verdict, delta.by_kind, delta.seconds_to_limit), ("stable", {}, None))
        self.assertNotIn("rate", main.render_delta(delta))


class RenderTests(FakeProc):
    def test_render_shows_usage_repeats_deleted_and_warns_near_the_limit(self):
        snap = self.typical()
        text = main.render(snap)
        self.assertIn("pid 4242 (worker): 10 open file descriptors, soft limit 1024 (1.0%), hard limit 524288", text)
        self.assertIn("same path open more than once:", text)
        self.assertIn("     2  /dev/pts/3", text)
        self.assertIn("deleted but still open", text)
        self.assertIn("gone.log", text)
        self.assertNotIn("WARNING", text)
        snap.soft_limit = 12
        self.assertIn("WARNING: within 17% of the soft limit", main.render(snap))

    def test_descriptors_without_cloexec_are_called_out(self):
        make_proc(self.root, 7, "legacy", {0: "/dev/null", 3: self.log, 4: self.log}, flags="02")
        snap = main.snapshot(7, self.root)
        self.assertEqual([d.fd for d in main.without_cloexec(snap)], [3, 4])
        self.assertIn("2 descriptor(s) without O_CLOEXEC", main.render(snap))


class ScanAndCliTests(FakeProc):
    def test_scan_deleted_covers_every_readable_process(self):
        self.typical(pid=100)
        make_proc(self.root, 200, "clean", {0: "/dev/null"})
        os.makedirs(os.path.join(self.root, "300"))
        write(os.path.join(self.root, "self"), "")
        found = main.scan_deleted(self.root)
        self.assertEqual([(f["pid"], f["fd"], f["comm"]) for f in found], [(100, 9, "worker")])
        self.assertIn("1 deleted file(s) still held open", main.render_deleted(found))
        self.assertEqual(main.render_deleted([]), "no deleted-but-open files")

    def test_main_json_report_from_a_root(self):
        self.typical(pid=55)
        from io import StringIO
        from contextlib import redirect_stdout
        out = StringIO()
        with redirect_stdout(out):
            code = main.main(["55", "--json", "--root", self.root])
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual((report["pid"], report["count"], report["soft_limit"]), (55, 10, 1024))
        self.assertEqual(report["kinds"]["socket"], 1)
        self.assertEqual(report["deleted"][0]["fd"], 9)
        self.assertEqual(len(report["descriptors"]), 10)

    def test_main_fails_cleanly_without_procfs_or_process(self):
        from io import StringIO
        from contextlib import redirect_stderr
        err = StringIO()
        with redirect_stderr(err):
            self.assertEqual(main.main(["1", "--root", os.path.join(self.root, "nope")]), 2)
            self.assertEqual(main.main(["123456", "--root", self.root]), 1)
        self.assertIn("needs Linux procfs", err.getvalue())
        self.assertIn("no readable process 123456", err.getvalue())

    def test_watch_emits_one_delta_per_round_with_fake_sleep(self):
        snaps = iter([self.typical(pid=1), self.typical(pid=2, extra={20: self.log}), self.typical(pid=3, extra={20: self.log, 21: self.log})])
        slept, emitted = [], []
        deltas = main.watch(lambda: next(snaps), 2.5, 2, sleep=slept.append, emit=emitted.append)
        self.assertEqual(slept, [2.5, 2.5])
        self.assertEqual([d.after for d in deltas], [11, 12])
        self.assertEqual(len(emitted), 3)
        self.assertTrue(emitted[0].startswith("pid 1 (worker): 10 open"))


if __name__ == "__main__":
    unittest.main()
