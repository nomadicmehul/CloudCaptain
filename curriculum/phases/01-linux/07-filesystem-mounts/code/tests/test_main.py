import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
HOST = os.path.join(FIXTURES, "host.mountinfo")
CONTAINER = os.path.join(FIXTURES, "container.mountinfo")
UNSHARED = os.path.join(FIXTURES, "host-unshare.mountinfo")

NESTED = """27 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw
50 27 0:40 / /mnt/x/y rw,relatime shared:30 - tmpfs tmpfs rw,size=10240k
51 27 0:41 / /mnt/x rw,relatime shared:31 - tmpfs tmpfs rw,size=10240k
"""

SESSIONS = """27 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw
85 27 8:16 / /var/lib/sessions rw,noatime shared:41 - ext4 /dev/sdb rw
"""


def fake_statvfs(table):
    def statvfs(path):
        if path not in table:
            raise OSError(2, "No such file or directory")
        blocks, bfree, bavail, files, ffree = table[path]
        return SimpleNamespace(f_bsize=4096, f_frsize=4096, f_blocks=blocks, f_bfree=bfree, f_bavail=bavail, f_files=files, f_ffree=ffree, f_favail=ffree)

    return statvfs


def run_main(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class ParseTests(unittest.TestCase):
    def test_parse_line_splits_the_fields_and_unescapes_the_mount_point(self):
        m = main.parse_line("130 27 8:33 / /mnt/backup\\040disk rw,relatime shared:70 - ext4 /dev/sdc1 rw", 3)
        self.assertEqual((m.mount_id, m.parent_id, m.device, m.root), (130, 27, "8:33", "/"))
        self.assertEqual(m.mount_point, "/mnt/backup disk")
        self.assertEqual(m.options, ["rw", "relatime"])
        self.assertEqual(m.optional, ["shared:70"])
        self.assertEqual((m.fstype, m.source, m.super_options, m.order), ("ext4", "/dev/sdc1", ["rw"], 3))
        self.assertEqual((m.read_only, m.is_bind, m.label), (False, False, "/dev/sdc1"))

    def test_parse_line_optional_fields_propagation_and_odd_roots(self):
        both = main.parse_line("36 35 98:0 /mnt1 /mnt2 rw,noatime shared:5 master:1 - ext3 /dev/root rw,errors=continue")
        self.assertEqual(both.optional, ["shared:5", "master:1"])
        self.assertEqual((both.propagation, both.peer_group, both.master_group), ("shared,slave", 5, 1))
        self.assertEqual((both.is_bind, both.label), (True, "/dev/root[/mnt1]"))
        private = main.parse_line("1498 1497 0:66 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw")
        self.assertEqual((private.optional, private.propagation, private.peer_group), ([], "private", None))
        self.assertTrue(private.has("noexec"))
        nsfs = main.parse_line("121 25 0:3 net:[4026532611] /run/docker/netns/5a1b rw shared:65 - nsfs nsfs rw")
        self.assertEqual((nsfs.root, nsfs.label), ("net:[4026532611]", "nsfs[net:[4026532611]]"))
        self.assertEqual(main.parse_line("40 27 0:32 / /x rw unbindable - tmpfs tmpfs rw").propagation, "unbindable")
        self.assertEqual(main.parse_line("40 27 0:32 / /x rw - tmpfs tmpfs").super_options, [])
        with self.assertRaises(ValueError):
            main.parse_line("12 1 0:3 / /x rw shared:1 ext4 /dev/sda rw")
        with self.assertRaises(ValueError):
            main.parse_line("12 1 0:3 / /x rw -")
        self.assertEqual(main.unescape("a\\040b\\011c\\134d"), "a b\tc\\d")

    def test_fixtures_parse_with_the_expected_roots(self):
        host = main.load(HOST)
        container = main.load(CONTAINER)
        self.assertEqual(len(host), 29)
        self.assertEqual([m.mount_point for m in main.roots(host)], ["/"])
        self.assertEqual([m.mount_id for m in main.roots(container)], [1497])
        root = main.find_mount(host, "/")
        self.assertEqual((root.mount_id, root.fstype, root.source, root.propagation, root.peer_group), (27, "ext4", "/dev/nvme0n1p2", "shared", 1))
        self.assertEqual((container[0].propagation, container[0].master_group, container[0].read_only), ("slave", 64, True))
        self.assertEqual([m.order for m in host[:3]], [0, 1, 2])


class TreeTests(unittest.TestCase):
    def test_walk_is_depth_first_in_mount_order_and_covers_every_row(self):
        host = main.load(HOST)
        walked = [(depth, m.mount_point) for depth, m in main.walk(host)]
        self.assertEqual(walked[:3], [(0, "/"), (1, "/sys"), (2, "/sys/kernel/security")])
        self.assertIn((2, "/run/user/1000"), walked)
        self.assertIn((1, "/mnt/scratch"), walked)
        self.assertIn((2, "/mnt/scratch"), walked)
        self.assertEqual(len(walked), len(host))
        self.assertEqual([(d, m.mount_point) for d, m in main.walk(main.load(CONTAINER))][:3], [(0, "/"), (1, "/proc"), (2, "/proc/bus")])

    def test_render_tree_columns_notes_and_short_labels(self):
        text = main.render_tree(main.load(CONTAINER))
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("TARGET") and "PROPAGATION" in lines[0] and lines[0].endswith("NOTE"))
        self.assertRegex(lines[1], r"^/ +overlay +overlay +ro,relatime +slave +3 lower layer\(s\) \+ upperdir$")
        self.assertIn("/dev/sdb[/exports]", text)
        self.assertIn("/dev/nvme0n1p2[.../hosts]", text)
        self.assertNotIn("3d2f8a1b9c7e", text)
        host_text = main.render_tree(main.load(HOST))
        self.assertIn("shadowed by mount 108", host_text)
        self.assertIn("/mnt/backup disk", host_text)
        self.assertIn("nsfs[net:[4026532611]]", host_text)


class WhichTests(unittest.TestCase):
    def setUp(self):
        self.host = main.load(HOST)
        self.container = main.load(CONTAINER)

    def test_longest_visible_mount_point_wins(self):
        self.assertEqual(main.find_mount(self.host, "/data/exports/report.csv").mount_point, "/data")
        self.assertEqual(main.find_mount(self.host, "/proc/self/status").mount_point, "/proc")
        self.assertEqual(main.find_mount(self.host, "/proc/sys/fs/binfmt_misc/python3").fstype, "autofs")
        self.assertEqual(main.find_mount(self.host, "/datastore/x").mount_point, "/")
        self.assertEqual(main.find_mount(self.host, "/mnt/scratch/x").mount_id, 108)
        self.assertEqual(main.find_mount(self.host, "/mnt/backup disk/x").source, "/dev/sdc1")
        self.assertEqual(main.find_mount(self.host, "/srv/exports/../exports/a").mount_point, "/srv/exports")
        with self.assertRaises(LookupError):
            main.find_mount([m for m in self.host if m.mount_point != "/"], "/nowhere")

    def test_locate_reports_the_bind_root_and_per_mount_flags(self):
        loc = main.locate(self.host, "/srv/exports/q1/report.csv")
        self.assertEqual((loc.mount_point, loc.mount_id, loc.path_in_mount), ("/srv/exports", 91, "q1/report.csv"))
        self.assertEqual((loc.source, loc.fstype, loc.device), ("/dev/sdb", "xfs", "8:16"))
        self.assertEqual((loc.bind, loc.root, loc.path_on_source), (True, "/exports", "/exports/q1/report.csv"))
        self.assertEqual((loc.read_only, loc.noexec, loc.nosuid, loc.nodev, loc.propagation), (True, False, False, False, "shared"))
        self.assertIn("rw", loc.super_options)
        self.assertIsNone(loc.overlay)
        text = main.render_location(loc)
        self.assertIn("bind of      /exports on that filesystem, so the path is /exports/q1/report.csv there", text)
        self.assertIn("ro: yes  noexec: no  nosuid: no  nodev: no", text)
        self.assertIn("ro is set on this mount point only", text)
        rw = main.locate(self.host, "/data/exports/q1/report.csv")
        self.assertEqual((rw.read_only, rw.bind, rw.path_on_source), (False, False, "/exports/q1/report.csv"))
        self.assertNotIn("note", main.render_location(rw))

    def test_locate_inside_a_container(self):
        root = main.locate(self.container, "/usr/bin/python3")
        self.assertEqual((root.mount_point, root.fstype, root.read_only, root.propagation, root.path_in_mount), ("/", "overlay", True, "slave", "usr/bin/python3"))
        self.assertEqual(len(root.overlay.lower), 3)
        self.assertTrue(root.overlay.upper.endswith("/diff"))
        self.assertIn("overlay      3 lower layer(s) + upperdir (writable layer); mounted ro", main.render_location(root))
        tmp = main.locate(self.container, "/tmp/tool")
        self.assertEqual((tmp.fstype, tmp.noexec, tmp.nosuid, tmp.nodev, tmp.read_only), ("tmpfs", True, True, True, False))
        self.assertIn("noexec: binaries and scripts under this mount cannot be executed", main.render_location(tmp))
        data = main.locate(self.container, "/data")
        self.assertEqual((data.path_in_mount, data.bind, data.root, data.read_only), (".", True, "/exports", True))
        masked = main.locate(self.container, "/proc/kcore")
        self.assertEqual((masked.fstype, masked.root, masked.mount_id), ("tmpfs", "/null", 1515))
        self.assertTrue(main.locate(self.container, "/proc/sys/kernel/hostname").read_only)
        self.assertFalse(main.locate(self.container, "/proc/cpuinfo").read_only)
        self.assertEqual(main.locate(self.container, "/etc/hosts").path_on_source.split("/")[-1], "hosts")


class ShadowTests(unittest.TestCase):
    def test_stacked_mount_on_the_same_path_hides_the_earlier_one(self):
        host = main.load(HOST)
        hidden = main.shadowed(host)
        self.assertEqual([(m.mount_id, by.mount_id) for m, by in hidden], [(104, 108)])
        self.assertNotIn(104, [m.mount_id for m in main.visible(host)])
        self.assertEqual(len(main.visible(host)), 28)
        text = main.render_shadowed(hidden)
        self.assertIn("1 mount(s) hidden by later mounts", text)
        self.assertIn("/mnt/scratch  tmpfs tmpfs (mount 104)  hidden by  /mnt/scratch  /dev/sdb[/scratch] xfs (mount 108)", text)
        self.assertEqual(main.shadowed(main.load(CONTAINER)), [])
        self.assertEqual(main.render_shadowed([]), "no shadowed mounts: every mount is reachable by path")

    def test_later_mount_on_a_covering_path_hides_the_subtree_but_ancestors_do_not(self):
        mounts = main.parse_mountinfo(NESTED)
        self.assertEqual([(m.mount_id, by.mount_id) for m, by in main.shadowed(mounts)], [(50, 51)])
        self.assertEqual(main.find_mount(mounts, "/mnt/x/y/file").mount_id, 51)
        root_last = main.parse_mountinfo("\n".join(NESTED.splitlines()[1:] + NESTED.splitlines()[:1]))
        self.assertEqual([(m.mount_id, by.mount_id) for m, by in main.shadowed(root_last)], [(50, 51)])
        reordered = main.parse_mountinfo("\n".join(reversed(NESTED.splitlines())))
        self.assertEqual(main.shadowed(reordered), [])


class OverlayTests(unittest.TestCase):
    def test_overlay_layers_upper_and_work_from_the_superblock_options(self):
        found = main.overlays(main.load(CONTAINER))
        self.assertEqual(len(found), 1)
        overlay = found[0]
        self.assertEqual((overlay.mount_point, len(overlay.lower), overlay.read_only, overlay.writable), ("/", 3, True, False))
        self.assertEqual(overlay.lower[0], "/var/lib/docker/overlay2/l/QW3RTY7UIOP2ASDFGHJKL5ZXC")
        self.assertTrue(overlay.upper.endswith("/diff") and overlay.work.endswith("/work"))
        text = main.render_overlays(found)
        self.assertIn("/: 3 lower layer(s), read-only (ro flag)", text)
        self.assertIn("lower[2]  /var/lib/docker/overlay2/l/ZXC6VBNMASDFGHJKLQWERTYUI", text)
        self.assertIn("upperdir  /var/lib/docker/overlay2/9f4c", text)
        host = main.overlays(main.load(HOST))
        self.assertEqual(len(host), 1)
        self.assertTrue(host[0].mount_point.endswith("/merged") and host[0].writable)
        self.assertIsNone(main.overlay_of(main.load(HOST)[0]))

    def test_overlay_without_upperdir_is_read_only_by_construction(self):
        m = main.parse_line("200 27 0:80 / /mnt/ro rw,relatime - overlay overlay rw,lowerdir=/a:/b\\040c")
        overlay = main.overlay_of(m)
        self.assertEqual((overlay.lower, overlay.upper, overlay.work, overlay.read_only, overlay.writable), (["/a", "/b c"], None, None, False, False))
        self.assertIn("/mnt/ro: 2 lower layer(s), read-only (no upperdir)", main.render_overlays([overlay]))
        self.assertEqual(main.render_overlays([]), "no overlay mounts")


class UsageTests(unittest.TestCase):
    TABLE = {
        "/": (25000000, 14000000, 12700000, 6553600, 6100000),
        "/boot/efi": (130000, 120000, 120000, 0, 0),
        "/data": (250000000, 100000000, 100000000, 52428800, 52400000),
        "/run": (400000, 399000, 399000, 400000, 399000),
        "/run/lock": (1280, 1280, 1280, 400000, 399999),
        "/run/user/1000": (200000, 200000, 200000, 204169, 204100),
        "/dev/shm": (2000000, 2000000, 2000000, 2000000, 1999999),
        "/mnt/scratch": (250000000, 100000000, 100000000, 52428800, 52400000),
        "/snap/core22/1963": (19000, 0, 0, 12000, 0),
    }

    def test_usage_skips_pseudo_filesystems_hidden_mounts_and_repeated_devices(self):
        host = main.load(HOST)
        report = main.usage(host, statvfs=fake_statvfs(self.TABLE))
        points = [u.mount_point for u in report]
        for pseudo in ("/proc", "/sys", "/sys/fs/cgroup", "/dev", "/dev/pts", "/run/docker/netns/5a1b2c3d4e5f"):
            self.assertNotIn(pseudo, points)
        self.assertIn("/data", points)
        self.assertNotIn("/srv/exports", points)
        self.assertNotIn("/mnt/scratch", points)
        root = report[points.index("/")]
        self.assertEqual((root.size, root.used, root.avail), (25000000 * 4096, 11000000 * 4096, 12700000 * 4096))
        self.assertAlmostEqual(root.use_percent, 11000000 / 23700000 * 100)
        self.assertEqual((root.inodes, root.inodes_used, root.inodes_avail), (6553600, 453600, 6100000))
        self.assertEqual(report[points.index("/mnt/backup disk")].error, "No such file or directory")
        self.assertEqual(report[points.index("/boot/efi")].inode_percent, 0.0)
        snap = report[points.index("/snap/core22/1963")]
        self.assertEqual((snap.read_only, snap.use_percent, snap.inode_percent), (True, 100.0, 100.0))
        everything = main.usage(host, statvfs=fake_statvfs(self.TABLE), include_all=True)
        all_points = [u.mount_point for u in everything]
        self.assertIn("/proc", all_points)
        self.assertIn("/srv/exports", all_points)
        self.assertNotIn(104, [u for u in all_points if u == 104])
        text = main.render_usage(report)
        self.assertIn("IUSE%", text)
        self.assertIn("unavailable: No such file or directory", text)
        self.assertEqual(main.usage_warnings(report), [])

    def test_inode_exhaustion_is_named_when_bytes_are_free(self):
        mounts = main.parse_mountinfo(SESSIONS)
        table = {"/": (25000000, 14000000, 12700000, 6553600, 6100000), "/var/lib/sessions": (25000000, 24000000, 23000000, 1638400, 120)}
        report = main.usage(mounts, statvfs=fake_statvfs(table))
        warnings = main.usage_warnings(report)
        self.assertEqual(len(warnings), 1)
        self.assertIn("WARNING: /var/lib/sessions has 120 of 1638400 inodes free (100% used) with 96% of its bytes free", warnings[0])
        self.assertIn("inode exhaustion", warnings[0])
        self.assertIn("WARNING: /var/lib/sessions", main.render_usage(report))
        full = main.usage(mounts, statvfs=fake_statvfs({"/": (1000, 50, 50, 100000, 90000), "/var/lib/sessions": (1000, 500, 500, 100, 50)}))
        self.assertEqual(main.usage_warnings(full), ["WARNING: / is 95% full (200.0 KB left)"])
        self.assertEqual(main.human_size(0), "0 B")
        self.assertEqual(main.human_size(1536), "1.5 KB")
        self.assertEqual(main.human_size(3 * 1024 ** 4), "3.0 TB")


class DiffTests(unittest.TestCase):
    def test_diff_reports_added_removed_and_changed_rows(self):
        result = main.diff(main.load(HOST), main.load(UNSHARED))
        self.assertEqual([m.mount_point for m in result.added], ["/mnt/jail"])
        self.assertTrue(result.added[0].read_only and result.added[0].is_bind)
        self.assertEqual(result.removed, [])
        changes = {(c.mount_point, c.what): (c.before, c.after) for c in result.changed}
        self.assertEqual(changes[("/data", "options")], ("rw,noatime", "ro,noatime"))
        self.assertEqual(changes[("/", "propagation")], ("shared", "private"))
        self.assertEqual(changes[("/mnt/scratch", "fstype")], ("xfs", "tmpfs"))
        self.assertEqual(changes[("/mnt/scratch", "source")], ("/dev/sdb[/scratch]", "tmpfs"))
        self.assertEqual(sum(1 for c in result.changed if c.what == "propagation"), 28)
        text = main.render_diff(result)
        self.assertIn("mountinfo diff: 1 added, 0 removed, 32 changed", text)
        self.assertIn("+ /mnt/jail  /dev/sdb[/exports]  xfs  ro,noatime  private", text)
        self.assertIn("~ propagation: shared -> private on 28 mounts (/sys, /proc, /dev, ...)", text)
        self.assertIn("~ /data  options: rw,noatime -> ro,noatime", text)
        self.assertEqual(main.diff(main.load(HOST), main.load(HOST)), main.Diff())

    def test_diff_container_against_host_shows_what_the_runtime_did(self):
        result = main.diff(main.load(HOST), main.load(CONTAINER))
        added = {m.mount_point for m in result.added}
        self.assertTrue({"/tmp", "/etc/hosts", "/proc/kcore", "/proc/sys", "/sys/firmware"} <= added)
        removed = {m.mount_point for m in result.removed}
        self.assertTrue({"/boot/efi", "/srv/exports", "/run"} <= removed)
        data_changes = {c.what: (c.before, c.after) for c in result.changed if c.mount_point == "/data"}
        self.assertEqual(data_changes, {"source": ("/dev/sdb", "/dev/sdb[/exports]"), "options": ("rw,noatime", "ro,noatime"), "propagation": ("shared", "private")})
        root_changes = {c.what: (c.before, c.after) for c in result.changed if c.mount_point == "/"}
        self.assertEqual(root_changes, {"source": ("/dev/nvme0n1p2", "overlay"), "fstype": ("ext4", "overlay"), "options": ("rw,relatime", "ro,relatime"), "propagation": ("shared", "slave")})
        self.assertIn(("/sys", "options"), {(c.mount_point, c.what) for c in result.changed})
        text = main.render_diff(result)
        self.assertIn("- /boot/efi  /dev/nvme0n1p1  vfat  rw,relatime  shared", text)
        self.assertIn("~ /  fstype: ext4 -> overlay", text)


class CliTests(unittest.TestCase):
    def test_which_json_from_a_saved_file(self):
        code, out, err = run_main(["which", "/data/report.csv", "--file", CONTAINER, "--json"])
        self.assertEqual((code, err), (0, ""))
        d = json.loads(out)
        self.assertEqual((d["mount_point"], d["read_only"], d["bind"], d["root"], d["path_on_source"]), ("/data", True, True, "/exports", "/exports/report.csv"))
        self.assertIsNone(d["overlay"])
        code, out, _ = run_main(["which", "/", "--file", CONTAINER, "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["overlay"]["lower"][0].endswith("QW3RTY7UIOP2ASDFGHJKL5ZXC"))
        code, out, _ = run_main(["which", "/tmp/tool", "--file", CONTAINER])
        self.assertIn("noexec: yes", out)

    def test_tree_overlay_shadowed_usage_and_diff_commands(self):
        code, out, _ = run_main(["tree", "--file", HOST, "--json"])
        rows = json.loads(out)
        self.assertEqual((code, len(rows)), (0, 29))
        self.assertEqual((rows[5]["mount_point"], rows[5]["peer_group"], rows[5]["propagation"], rows[5]["bind"]), ("/", 1, "shared", False))
        code, out, _ = run_main(["tree", "--file", CONTAINER])
        self.assertTrue(out.startswith("TARGET"))
        code, out, _ = run_main(["overlay", "--file", CONTAINER, "--json"])
        self.assertEqual(len(json.loads(out)[0]["lower"]), 3)
        code, out, _ = run_main(["shadowed", "--file", HOST, "--json"])
        self.assertEqual((json.loads(out)[0]["hidden"]["mount_id"], json.loads(out)[0]["by"]["mount_id"]), (104, 108))
        code, out, _ = run_main(["diff", HOST, UNSHARED, "--json"])
        d = json.loads(out)
        self.assertEqual((code, d["added"][0]["mount_point"], len(d["changed"])), (0, "/mnt/jail", 32))
        code, out, _ = run_main(["diff", HOST, UNSHARED])
        self.assertIn("mountinfo diff: 1 added", out)
        code, out, _ = run_main(["usage", "--file", CONTAINER, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(out)), ["mounts", "warnings"])

    def test_exit_codes_and_messages(self):
        code, _, err = run_main(["tree", "--file", os.path.join(FIXTURES, "missing.mountinfo")])
        self.assertEqual(code, 2)
        self.assertIn("not found", err)
        with tempfile.NamedTemporaryFile("w", suffix=".mountinfo", delete=False) as handle:
            handle.write("50 27 0:40 / /mnt/x rw,relatime - tmpfs tmpfs rw\n")
        self.addCleanup(os.unlink, handle.name)
        code, _, err = run_main(["which", "/nowhere", "--file", handle.name])
        self.assertEqual(code, 1)
        self.assertIn("no mount covers /nowhere", err)
        with open(handle.name, "w") as broken:
            broken.write("garbage line without separator\n")
        code, _, err = run_main(["tree", "--file", handle.name])
        self.assertEqual(code, 1)
        self.assertIn("separator", err)
        code, out, _ = run_main([])
        self.assertEqual(code, 2)
        self.assertIn("usage: mountlens", out)
        code, _, err = run_main(["tree"])
        if os.path.exists(main.MOUNTINFO):
            self.assertEqual(code, 0)
        else:
            self.assertEqual(code, 2)
            self.assertIn("needs Linux procfs", err)


if __name__ == "__main__":
    unittest.main()
