import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

SUBUID = """# subordinate id ranges, one per line: OWNER:START:COUNT
alice:100000:65536
bob:165536:65536

alice:231072:65536
1001:296608:65536
"""

PODMAN_UID_MAP = "         0       1000          1\n         1     100000      65536\n"
INITIAL_MAP = "         0          0 4294967295\n"


def write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class SubidParsingTests(unittest.TestCase):
    def test_parse_subid_skips_comments_and_blank_lines_and_keeps_file_order(self):
        ranges = main.parse_subid(SUBUID)
        self.assertEqual(
            [(r.owner, r.start, r.count) for r in ranges],
            [("alice", 100000, 65536), ("bob", 165536, 65536), ("alice", 231072, 65536), ("1001", 296608, 65536)],
        )
        self.assertEqual(ranges[0].last, 165535)

    def test_parse_subid_rejects_malformed_lines_with_the_line_number(self):
        with self.assertRaises(ValueError) as ctx:
            main.parse_subid("alice:100000:65536\nbob:abc:10\n", "/etc/subuid")
        self.assertIn("/etc/subuid:2", str(ctx.exception))
        with self.assertRaises(ValueError):
            main.parse_subid("alice:100000:0\n")
        with self.assertRaises(ValueError):
            main.parse_subid("alice:4294967000:1000\n")

    def test_ranges_for_matches_the_name_or_the_numeric_uid(self):
        ranges = main.parse_subid(SUBUID)
        self.assertEqual([r.start for r in main.ranges_for(ranges, "alice", 1000)], [100000, 231072])
        self.assertEqual([r.start for r in main.ranges_for(ranges, "carol", 1001)], [296608])
        self.assertEqual(main.ranges_for(ranges, "dave", 1002), [])

    def test_suggest_range_starts_after_the_highest_allocated_id(self):
        self.assertEqual(main.suggest_range([]), main.SubidRange("", 100000, 65536))
        suggestion = main.suggest_range(main.parse_subid(SUBUID))
        self.assertEqual((suggestion.start, suggestion.last), (362144, 427679))


class MappingTests(unittest.TestCase):
    def test_build_mapping_puts_the_user_at_0_then_each_range_contiguously(self):
        ranges = main.ranges_for(main.parse_subid(SUBUID), "alice", 1000)
        entries = main.build_mapping(1000, ranges)
        self.assertEqual(entries, [main.MapEntry(0, 1000, 1), main.MapEntry(1, 100000, 65536), main.MapEntry(65537, 231072, 65536)])
        self.assertEqual(main.mapped_count(entries), 131073)
        self.assertEqual(main.build_mapping(1002, []), [main.MapEntry(0, 1002, 1)])

    def test_validate_mapping_rejects_overlaps_bad_counts_and_too_many_lines(self):
        with self.assertRaises(ValueError) as ctx:
            main.validate_mapping([main.MapEntry(0, 1000, 1), main.MapEntry(0, 100000, 10)])
        self.assertIn("inside ranges overlap", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            main.validate_mapping([main.MapEntry(0, 1000, 1), main.MapEntry(1, 1000, 10)])
        self.assertIn("outside ranges overlap", str(ctx.exception))
        with self.assertRaises(ValueError):
            main.validate_mapping([main.MapEntry(0, 1000, 0)])
        with self.assertRaises(ValueError):
            main.validate_mapping([main.MapEntry(i, i, 1) for i in range(341)])
        main.validate_mapping([main.MapEntry(i, i, 1) for i in range(340)])

    def test_render_id_map_uses_the_kernel_column_format_and_round_trips(self):
        entries = main.parse_id_map(PODMAN_UID_MAP)
        self.assertEqual(entries, [main.MapEntry(0, 1000, 1), main.MapEntry(1, 100000, 65536)])
        self.assertEqual(main.render_id_map(entries), PODMAN_UID_MAP)
        self.assertTrue(main.is_initial(main.parse_id_map(INITIAL_MAP)))
        self.assertFalse(main.is_initial(entries))
        with self.assertRaises(ValueError) as ctx:
            main.parse_id_map("0 1000\n", "/proc/42/uid_map")
        self.assertIn("/proc/42/uid_map:1", str(ctx.exception))

    def test_newidmap_args_flatten_the_map_after_the_pid(self):
        entries = main.parse_id_map(PODMAN_UID_MAP)
        self.assertEqual(main.newidmap_args("newuidmap", 4242, entries), ["newuidmap", "4242", "0", "1000", "1", "1", "100000", "65536"])

    def test_translate_container_ids_to_host_ids_and_back(self):
        entries = main.parse_id_map(PODMAN_UID_MAP)
        self.assertEqual(main.to_host(entries, 0), 1000)
        self.assertEqual(main.to_host(entries, 1), 100000)
        self.assertEqual(main.to_host(entries, 999), 100998)
        self.assertEqual(main.to_host(entries, 65536), 165535)
        self.assertIsNone(main.to_host(entries, 65537))
        self.assertEqual(main.to_container(entries, 1000), 0)
        self.assertEqual(main.to_container(entries, 100999), 1000)
        self.assertIsNone(main.to_container(entries, 0))
        self.assertIsNone(main.to_container(entries, 1001))
        results = main.translate_ids(entries, [999, 70000], from_host=False)
        self.assertEqual([r["mapped"] for r in results], [100998, None])
        self.assertEqual(results[1]["text"], "container uid 70000 -> unmapped (no process or file inside can have it)")
        self.assertEqual(main.translate_ids(entries, [0], from_host=True)[0]["text"], "host uid 0 -> unmapped (appears inside as 65534)")

    def test_parse_map_option_builds_and_validates_entries(self):
        self.assertEqual(main.parse_map_option([["0", "1000", "1"], ["1", "100000", "65536"]]), main.parse_id_map(PODMAN_UID_MAP))
        with self.assertRaises(ValueError):
            main.parse_map_option([["0", "x", "1"]])


class DescribeTests(unittest.TestCase):
    def test_describe_rootless_map_names_root_ranges_overflow_and_limits(self):
        notes = main.describe(main.parse_id_map(PODMAN_UID_MAP), "uid", own_id=1000, own_name="alice")
        self.assertEqual(
            notes,
            [
                "container uid 0 is host uid 1000 (alice)",
                "container uids 1..65536 are host uids 100000..165535",
                "host uid 0 is unmapped: inside it appears as 65534 (the overflow id, usually nobody) and nothing can chown to it",
                "container uids above 65536 are unmapped: no process can hold them and no file can be owned by them",
                "65537 ids in 2 line(s) (kernel limit 340)",
            ],
        )

    def test_describe_and_verdict_for_the_initial_namespace_and_the_single_user_map(self):
        initial = main.parse_id_map(INITIAL_MAP)
        self.assertEqual(len(main.describe(initial)), 1)
        self.assertIn("not rootless", main.verdict(initial))
        single = [main.MapEntry(0, 1000, 1)]
        self.assertIn("unshare -Ur", main.verdict(single))
        self.assertIn("65536 more uids", main.verdict(main.parse_id_map(PODMAN_UID_MAP)))

    def test_single_user_map_writes_setgroups_deny_before_gid_map(self):
        files = main.single_user_map(1000, 1000)
        self.assertEqual([path for path, _ in files], ["/proc/self/setgroups", "/proc/self/uid_map", "/proc/self/gid_map"])
        self.assertEqual(files[0][1], "deny")
        self.assertEqual(files[1][1], "         0       1000          1\n")

    def test_parse_namespaces_maps_names_to_clone_flags(self):
        self.assertEqual(main.parse_namespaces(""), 0)
        self.assertEqual(main.parse_namespaces("mnt,pid"), main.CLONE_NEWNS | main.CLONE_NEWPID)
        with self.assertRaises(ValueError):
            main.parse_namespaces("mnt,bogus")


class FakeProc(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="userns-")
        self.addCleanup(shutil.rmtree, self.root)
        self.subuid = os.path.join(self.root, "subuid")
        self.subgid = os.path.join(self.root, "subgid")
        write(self.subuid, SUBUID)
        write(self.subgid, SUBUID)

    def make_proc(self, pid, uid_map, gid_map=None, overflow="65534"):
        proc = os.path.join(self.root, "proc")
        base = os.path.join(proc, str(pid))
        os.makedirs(base)
        write(os.path.join(base, "uid_map"), uid_map)
        if gid_map is not None:
            write(os.path.join(base, "gid_map"), gid_map)
        os.makedirs(os.path.join(proc, "sys", "kernel"), exist_ok=True)
        write(os.path.join(proc, "sys", "kernel", "overflowuid"), overflow + "\n")
        return proc

    def run_main(self, argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(argv)
        return code, out.getvalue(), err.getvalue()


class ExplainTests(FakeProc):
    def test_explain_process_reads_uid_map_gid_map_and_overflowuid(self):
        root = self.make_proc(4242, PODMAN_UID_MAP, PODMAN_UID_MAP, overflow="60000")
        report = main.explain_process(4242, root)
        self.assertEqual((report["label"], report["initial"], report["overflow_id"]), ("pid 4242", False, 60000))
        self.assertEqual(report["uid_map"][1], {"inside": 1, "outside": 100000, "count": 65536})
        self.assertIn("appears as 60000", report["uid_notes"][2])
        self.assertIn("gid 0 is host gid 1000", report["gid_notes"][0])
        text = main.render_report(report)
        self.assertIn("pid 4242: uid_map", text)
        self.assertIn("verdict: rootless with subordinate ids", text)

    def test_explain_missing_process_raises_a_helpful_error(self):
        root = self.make_proc(1, INITIAL_MAP)
        with self.assertRaises(FileNotFoundError) as ctx:
            main.explain_process(999, root)
        self.assertIn("run with sudo", str(ctx.exception))


class CliTests(FakeProc):
    def test_map_prints_the_plan_and_exits_0_when_both_ranges_exist(self):
        code, out, _ = self.run_main(["map", "--user", "alice", "--uid", "1000", "--gid", "1000", "--subuid", self.subuid, "--subgid", self.subgid, "--pid", "4242"])
        self.assertEqual(code, 0)
        self.assertIn("         1     100000      65536", out)
        self.assertIn("newuidmap 4242 0 1000 1 1 100000 65536 65537 231072 65536", out)
        self.assertIn("container uid 0 is host uid 1000 (alice)", out)
        self.assertIn("verdict: rootless with subordinate ids", out)

    def test_map_json_and_the_missing_range_case_exit_1_with_a_usermod_fix(self):
        code, out, _ = self.run_main(["map", "--user", "dave", "--uid", "1002", "--subuid", self.subuid, "--subgid", self.subgid, "--json"])
        self.assertEqual(code, 1)
        plan = json.loads(out)
        self.assertFalse(plan["complete"])
        self.assertEqual(plan["uid_map"], [{"inside": 0, "outside": 1002, "count": 1}])
        self.assertEqual(plan["gid"], 1002)
        self.assertEqual(plan["fix"], "sudo usermod --add-subuids 362144-427679 --add-subgids 362144-427679 dave && podman system migrate")
        code, out, _ = self.run_main(["map", "--user", "dave", "--uid", "1002", "--subuid", os.path.join(self.root, "missing"), "--subgid", self.subgid])
        self.assertEqual(code, 1)
        self.assertIn("/etc/subuid: file not found", out)

    def test_map_for_an_unknown_user_without_uid_is_a_usage_error(self):
        code, _, err = self.run_main(["map", "--user", "no-such-account-here", "--subuid", self.subuid, "--subgid", self.subgid])
        self.assertEqual(code, 2)
        self.assertIn("pass --uid", err)

    def test_translate_with_map_options_saved_files_and_subuid(self):
        code, out, _ = self.run_main(["translate", "--map", "0", "1000", "1", "--map", "1", "100000", "65536", "0", "1001"])
        self.assertEqual((code, out.splitlines()), (0, ["container uid 0 -> host uid 1000", "container uid 1001 -> host uid 101000"]))
        saved = os.path.join(self.root, "uid_map")
        write(saved, PODMAN_UID_MAP)
        code, out, _ = self.run_main(["translate", "--uid-map", saved, "--from-host", "100999", "0"])
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), ["host uid 100999 -> container uid 1000", "host uid 0 -> unmapped (appears inside as 65534)"])
        code, out, _ = self.run_main(["translate", "--user", "alice", "--uid", "1000", "--gid", "2000", "--subuid", self.subuid, "--subgid", self.subgid, "--kind", "gid", "--json", "0", "65537"])
        self.assertEqual(code, 0)
        self.assertEqual([r["mapped"] for r in json.loads(out)], [2000, 231072])

    def test_explain_cli_from_a_fake_proc_root_and_from_saved_files(self):
        root = self.make_proc(7, INITIAL_MAP, INITIAL_MAP)
        code, out, _ = self.run_main(["explain", "7", "--root", root, "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertTrue(report["initial"])
        self.assertIn("not rootless", report["verdict"])
        saved = os.path.join(self.root, "uid_map")
        write(saved, PODMAN_UID_MAP)
        code, out, _ = self.run_main(["explain", "--uid-map", saved])
        self.assertEqual(code, 0)
        self.assertIn("container uids 1..65536 are host uids 100000..165535", out)
        code, _, err = self.run_main(["explain", "--root", root])
        self.assertEqual(code, 2)
        self.assertIn("pass a PID", err)
        code, _, err = self.run_main(["explain", "99", "--root", root])
        self.assertEqual(code, 1)
        self.assertIn("no readable", err)

    def test_run_validates_namespaces_and_refuses_outside_linux(self):
        code, _, err = self.run_main(["run", "--ns", "bogus", "--", "true"])
        self.assertEqual(code, 2)
        self.assertIn("unknown namespace", err)
        with mock.patch.object(main.sys, "platform", "darwin"):
            code, _, err = self.run_main(["run", "--", "true"])
        self.assertEqual(code, 2)
        self.assertIn("needs Linux", err)


if __name__ == "__main__":
    unittest.main()
