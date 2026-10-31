import json
import os
import pwd
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

STATUS = """Name:\t{name}
Umask:\t0022
State:\tS (sleeping)
Tgid:\t{pid}
Ngid:\t0
Pid:\t{pid}
PPid:\t0
TracerPid:\t0
Uid:\t{uid}\t{uid}\t{uid}\t{uid}
Gid:\t{gid}\t{gid}\t{gid}\t{gid}
FDSize:\t64
Groups:\t{groups}
NStgid:\t{pid}
NSpid:\t{pid}
Threads:\t1
SigQ:\t0/15299
SigPnd:\t0000000000000000
CapInh:\t{inh}
CapPrm:\t{prm}
CapEff:\t{eff}
CapBnd:\t{bnd}
CapAmb:\t{amb}
NoNewPrivs:\t{nnp}
Seccomp:\t{seccomp}
Seccomp_filters:\t{filters}
Speculation_Store_Bypass:\tthread vulnerable
Cpus_allowed:\tf
"""

DOCKER_DEFAULT_NAMES = [
    "CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FOWNER", "CAP_FSETID", "CAP_KILL", "CAP_SETGID", "CAP_SETUID",
    "CAP_SETPCAP", "CAP_NET_BIND_SERVICE", "CAP_NET_RAW", "CAP_SYS_CHROOT", "CAP_MKNOD", "CAP_AUDIT_WRITE", "CAP_SETFCAP",
]
NET_BIND = 1 << 10


def status_text(name="nginx", pid=1, uid=0, gid=0, groups="0 1 2 3 4 6 10 11 20 26 27", inh="0000000000000000",
                prm="00000000a80425fb", eff="00000000a80425fb", bnd="00000000a80425fb", amb="0000000000000000",
                nnp=0, seccomp=2, filters=1):
    return STATUS.format(name=name, pid=pid, uid=uid, gid=gid, groups=groups, inh=inh, prm=prm, eff=eff, bnd=bnd,
                         amb=amb, nnp=nnp, seccomp=seccomp, filters=filters)


def entry(mode, owner="root", group="root", directory=False):
    return main.Entry(main.parse_mode(mode, directory), owner, group)


def who(user, *groups):
    return main.Identity(user, list(groups) or [user])


def run_main(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class CapabilityTableTests(unittest.TestCase):
    def test_table_has_41_unique_names_in_kernel_order(self):
        self.assertEqual(len(main.CAPABILITIES), 41)
        self.assertEqual(len(set(main.CAPABILITIES)), 41)
        for bit, name in ((0, "CAP_CHOWN"), (1, "CAP_DAC_OVERRIDE"), (7, "CAP_SETUID"), (10, "CAP_NET_BIND_SERVICE"),
                          (13, "CAP_NET_RAW"), (19, "CAP_SYS_PTRACE"), (21, "CAP_SYS_ADMIN"), (27, "CAP_MKNOD"),
                          (31, "CAP_SETFCAP"), (32, "CAP_MAC_OVERRIDE"), (37, "CAP_AUDIT_READ"), (38, "CAP_PERFMON"),
                          (39, "CAP_BPF"), (40, "CAP_CHECKPOINT_RESTORE")):
            self.assertEqual(main.CAPABILITIES[bit], name)
        self.assertEqual(main.ALL_CAPS, 0x000001FFFFFFFFFF)
        self.assertEqual(set(main.MEANINGS), set(main.CAPABILITIES))
        self.assertTrue(main.ROOT_EQUIVALENT <= set(main.CAPABILITIES))

    def test_decode_docker_default_all_and_unknown_bits(self):
        self.assertEqual(main.decode(main.DOCKER_DEFAULT), DOCKER_DEFAULT_NAMES)
        self.assertEqual(main.decode(main.ALL_CAPS), list(main.CAPABILITIES))
        self.assertEqual(main.decode(0), [])
        self.assertEqual(main.decode(1 << 41 | NET_BIND), ["CAP_NET_BIND_SERVICE", "bit41"])
        with self.assertRaises(ValueError):
            main.decode(-1)

    def test_parse_mask_accepts_hex_names_aliases_capsh_and_getpcaps_output(self):
        self.assertEqual(main.parse_mask("00000000a80425fb"), main.DOCKER_DEFAULT)
        self.assertEqual(main.parse_mask("0x400"), NET_BIND)
        self.assertEqual(main.parse_mask("400"), NET_BIND)
        self.assertEqual(main.parse_mask("cap_net_bind_service,CAP_CHOWN"), NET_BIND | 1)
        self.assertEqual(main.parse_mask("net_bind_service chown"), NET_BIND | 1)
        self.assertEqual(main.parse_mask("all"), main.ALL_CAPS)
        self.assertEqual(main.parse_mask("none"), 0)
        self.assertEqual(main.parse_mask("docker-default"), main.DOCKER_DEFAULT)
        self.assertEqual(main.parse_mask("0x00000000a80425fb=cap_chown,cap_dac_override"), main.DOCKER_DEFAULT)
        self.assertEqual(main.parse_mask("1234: cap_net_bind_service,cap_chown=ep"), NET_BIND | 1)

    def test_parse_mask_rejects_unknown_names_and_empty_input(self):
        with self.assertRaises(ValueError) as ctx:
            main.parse_mask("cap_fly")
        self.assertIn("unknown capability", str(ctx.exception))
        with self.assertRaises(ValueError):
            main.parse_mask("")

    def test_encode_round_trips_and_diff_masks(self):
        self.assertEqual(main.encode(main.decode(main.DOCKER_DEFAULT)), main.DOCKER_DEFAULT)
        self.assertEqual(main.encode(["net_bind_service"]), NET_BIND)
        d = main.diff_masks(main.DOCKER_DEFAULT, NET_BIND)
        self.assertEqual(len(d["only_a"]), 13)
        self.assertEqual(d["only_b"], [])
        self.assertEqual(d["both"], ["CAP_NET_BIND_SERVICE"])
        text = main.render_diff(main.DOCKER_DEFAULT, NET_BIND)
        self.assertIn("A 0x00000000a80425fb (14)  B 0x0000000000000400 (1)", text)
        self.assertIn("dropped going A -> B (13): CAP_CHOWN, CAP_DAC_OVERRIDE", text)
        self.assertIn("added going A -> B (0): none", text)
        self.assertIn("kept (1): CAP_NET_BIND_SERVICE", text)

    def test_render_caps_labels_and_root_equivalent_flags(self):
        self.assertEqual(main.render_caps(0), "0x0000000000000000: no capabilities: what --cap-drop ALL leaves")
        full = main.render_caps(main.ALL_CAPS)
        self.assertIn("all 41 capabilities: full root, what --privileged grants", full)
        self.assertIn("40  CAP_CHECKPOINT_RESTORE", full)
        one = main.render_caps(NET_BIND)
        self.assertIn("10  CAP_NET_BIND_SERVICE       bind to ports below 1024", one)
        self.assertIn("none of these is root-equivalent on its own", one)
        docker = main.render_caps(main.DOCKER_DEFAULT)
        self.assertIn("14 of 41 capabilities: the Docker default set", docker)
        self.assertIn("root-equivalent in practice (8, marked !): CAP_CHOWN, CAP_DAC_OVERRIDE", docker)
        self.assertNotIn("CAP_SYS_ADMIN", docker)


class ModeTests(unittest.TestCase):
    def test_parse_mode_from_octal_and_ls_strings(self):
        self.assertEqual((main.parse_mode("755").perm, main.parse_mode("755").kind), (0o755, "file"))
        self.assertTrue(main.parse_mode("4755").setuid)
        sticky = main.parse_mode("01777", directory=True)
        self.assertEqual((sticky.perm, sticky.kind, sticky.sticky), (0o1777, "directory", True))
        self.assertEqual(main.parse_mode("-rwsr-xr-x").perm, 0o4755)
        self.assertEqual(main.parse_mode("drwxrwxrwt").kind, "directory")
        self.assertEqual(main.parse_mode("drwxrwxrwt").perm, 0o1777)
        self.assertEqual(main.parse_mode("-rwxr-sr-x").perm, 0o2755)
        self.assertEqual(main.parse_mode("-rwSr--r--").perm, 0o4644)
        self.assertEqual(main.parse_mode("drwxrwxrwT").perm, 0o1776)
        self.assertEqual(main.parse_mode("drwxr-xr-x.").perm, 0o755)
        self.assertEqual(main.parse_mode("-rw-rw-r--+").perm, 0o664)
        self.assertEqual(main.parse_mode("lrwxrwxrwx").kind, "symlink")
        self.assertEqual((main.parse_mode("rw-r--r--").perm, main.parse_mode("rw-r--r--", directory=True).kind), (0o644, "directory"))
        self.assertEqual(main.parse_mode("0o644").perm, 0o644)

    def test_parse_mode_rejects_garbage(self):
        for bad in ("7778", "rwx", "8888", "77777", "", "-rwxr-xr-"):
            with self.assertRaises(ValueError, msg=bad):
                main.parse_mode(bad)

    def test_format_mode_round_trips_ls_strings(self):
        for text in ("-rwsr-xr-x", "drwxrwxrwt", "-rwxr-sr-x", "-rwSr--r--", "drwxrwxrwT", "drwxr-sr-x", "----------", "lrwxrwxrwx", "-rw-------"):
            self.assertEqual(main.format_mode(main.parse_mode(text)), text)
        self.assertEqual(main.format_mode(main.Mode(0o1777, "directory")), "drwxrwxrwt")
        self.assertEqual(main.triple(5), "r-x")


class AccessTests(unittest.TestCase):
    def test_owner_class_wins_even_when_group_bits_are_wider(self):
        d = main.access(entry("0070", owner="alice", group="dev"), who("alice", "alice", "dev"))
        self.assertEqual((d.klass, d.bits, d.read, d.write, d.execute, d.traverse), ("owner", "---", False, False, False, None))
        self.assertTrue(any("wider but do not apply" in n for n in d.notes), d.notes)

    def test_group_then_other_class(self):
        d = main.access(entry("0070", owner="alice", group="dev"), who("bob", "bob", "dev"))
        self.assertEqual((d.klass, d.read, d.write, d.execute), ("group", True, True, True))
        d = main.access(entry("0070", owner="alice", group="dev"), who("carol", "carol"))
        self.assertEqual((d.klass, d.read, d.write, d.execute), ("other", False, False, False))
        d = main.access(entry("0640", owner="root", group="shadow"), who("alice", "alice", "shadow"))
        self.assertEqual((d.klass, d.read, d.write), ("group", True, False))

    def test_directory_bits_mean_list_traverse_and_modify(self):
        d = main.access(entry("d-wx------", owner="alice"), who("alice"))
        self.assertEqual((d.read, d.traverse, d.write), (False, True, True))
        self.assertTrue(any("x without r" in n for n in d.notes), d.notes)
        d = main.access(entry("drw-------", owner="alice"), who("alice"))
        self.assertEqual((d.read, d.traverse, d.write), (True, False, False))
        self.assertTrue(any("w without x" in n for n in d.notes), d.notes)
        d = main.access(entry("drwxrwxrwt"), who("alice"))
        self.assertEqual((d.klass, d.write, d.traverse), ("other", True, True))
        self.assertTrue(any("delete only entries it owns" in n for n in d.notes), d.notes)
        d = main.access(entry("drwxrwxrwt"), who("root"))
        self.assertTrue(any("delete any entry" in n for n in d.notes), d.notes)
        d = main.access(entry("drwxrwsr-x", owner="root", group="dev"), who("bob", "bob", "dev"))
        self.assertTrue(any("new entries get group dev" in n for n in d.notes), d.notes)

    def test_setuid_setgid_and_execute_only_notes(self):
        d = main.access(entry("-rwsr-xr-x"), who("alice"))
        self.assertTrue(d.execute)
        self.assertTrue(any("runs with effective uid root" in n and "NoNewPrivs=1" in n for n in d.notes), d.notes)
        d = main.access(entry("-rwxr-sr-x", group="shadow"), who("alice"))
        self.assertTrue(any("effective gid shadow" in n for n in d.notes), d.notes)
        d = main.access(entry("-rwSr--r--"), who("alice"))
        self.assertFalse(d.execute)
        self.assertFalse(any("setuid" in n for n in d.notes), d.notes)
        d = main.access(entry("---x------", owner="alice"), who("alice"))
        self.assertEqual((d.read, d.execute), (False, True))
        self.assertTrue(any("a binary runs, a script does not" in n for n in d.notes), d.notes)
        d = main.access(entry("lrwxrwxrwx"), who("alice"))
        self.assertTrue(any("symlink" in n for n in d.notes), d.notes)

    def test_dac_override_read_search_and_fowner_change_the_answer(self):
        override = main.encode(["CAP_DAC_OVERRIDE"])
        search = main.encode(["CAP_DAC_READ_SEARCH"])
        d = main.access(entry("0600"), who("app"), override)
        self.assertEqual((d.read, d.write, d.execute), (True, True, False))
        self.assertTrue(any("execute needs at least one x bit" in n for n in d.notes), d.notes)
        d = main.access(entry("0700"), who("app"), override)
        self.assertTrue(d.execute)
        d = main.access(entry("0600"), who("app"), search)
        self.assertEqual((d.read, d.write, d.execute), (True, False, False))
        d = main.access(entry("drwx------"), who("app"), search)
        self.assertEqual((d.read, d.traverse, d.write), (True, True, False))
        d = main.access(entry("drwx------"), who("app"), override)
        self.assertEqual((d.read, d.traverse, d.write), (True, True, True))
        d = main.access(entry("drwxrwxrwt"), who("app"), main.encode(["CAP_FOWNER"]))
        self.assertTrue(any("delete any entry" in n for n in d.notes), d.notes)
        d = main.access(entry("0600"), who("app"), main.DOCKER_DEFAULT)
        self.assertEqual((d.read, d.write), (True, True))

    def test_render_access_shows_the_decision_and_notes(self):
        e = entry("-rwsr-xr-x")
        w = who("alice", "alice", "dev")
        text = main.render_access(e, w, main.access(e, w))
        self.assertIn("-rwsr-xr-x root root  (mode 4755, file) as alice (groups: alice, dev)", text)
        self.assertIn("  read      yes", text)
        self.assertIn("  write     no", text)
        self.assertIn("  execute   yes", text)
        self.assertIn("traverse on every parent directory", text)
        self.assertIn("  - setuid: runs with effective uid root", text)
        e = entry("drwxrwxrwt")
        text = main.render_access(e, w, main.access(e, w))
        self.assertIn("  modify    yes   (w and x: create, rename and delete entries)", text)

    def test_current_identity_and_entry_from_path_on_a_real_file(self):
        root = tempfile.mkdtemp(prefix="capcheck-")
        self.addCleanup(shutil.rmtree, root)
        path = os.path.join(root, "secret.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        os.chmod(path, 0o640)
        os.chmod(root, 0o311)
        me = main.current_identity()
        self.assertEqual(me.user, pwd.getpwuid(os.getuid()).pw_name)
        self.assertTrue(me.groups)
        e = main.entry_from_path(path)
        self.assertEqual((e.mode.perm, e.mode.kind, e.owner), (0o640, "file", me.user))
        d = main.access(e, me)
        self.assertEqual((d.klass, d.read, d.write, d.execute), ("owner", True, True, False))
        d = main.access(main.entry_from_path(root), me)
        self.assertEqual((d.read, d.traverse, d.write), (False, True, True))
        os.chmod(root, 0o755)


class StatusTests(unittest.TestCase):
    def test_parse_status_reads_identity_and_the_five_sets(self):
        s = main.parse_status(status_text())
        self.assertEqual((s.name, s.pid, s.uid, s.gid), ("nginx", 1, (0, 0, 0, 0), (0, 0, 0, 0)))
        self.assertEqual(s.groups[:3], [0, 1, 2])
        self.assertEqual((s.effective, s.permitted, s.bounding), (main.DOCKER_DEFAULT,) * 3)
        self.assertEqual((s.inheritable, s.ambient), (0, 0))
        self.assertEqual((s.no_new_privs, s.seccomp, s.seccomp_filters), (0, 2, 1))

    def test_parse_status_tolerates_missing_optional_fields_and_rejects_other_files(self):
        text = "\n".join(line for line in status_text(groups="").splitlines() if not line.startswith(("NoNewPrivs", "Seccomp_filters", "Groups")))
        s = main.parse_status(text + "\nGroups:\t\n")
        self.assertEqual((s.groups, s.no_new_privs, s.seccomp_filters, s.seccomp), ([], None, None, 2))
        with self.assertRaises(ValueError):
            main.parse_status("Name:\tbash\nPid:\t3\n")

    def test_verdicts_cover_root_dropped_root_and_non_root_cases(self):
        full = main.parse_status(status_text(prm="000001ffffffffff", eff="000001ffffffffff", bnd="000001ffffffffff", seccomp=0, filters=0))
        self.assertTrue(main.verdict(full).startswith("full root: every capability is effective"))
        dropped = main.parse_status(status_text(prm="0000000000000000", eff="0000000000000000", bnd="0000000000000000", nnp=1))
        self.assertIn("root in name only", main.verdict(dropped))
        default = main.parse_status(status_text())
        self.assertIn("uid 0 with 14 of 41 capabilities; root-equivalent bits present: CAP_CHOWN", main.verdict(default))
        bounded = main.parse_status(status_text(name="python3", pid=7, uid=1000, gid=1000, groups="", prm="0000000000000000", eff="0000000000000000", bnd="0000000000000400", nnp=0))
        self.assertEqual(main.verdict(bounded), "unprivileged uid 1000: permission bits decide everything; the bounding set would let file capabilities grant 1: CAP_NET_BIND_SERVICE")
        locked = main.parse_status(status_text(uid=1000, prm="0000000000000000", eff="0000000000000000", bnd="0000000000000400", nnp=1))
        self.assertIn("NoNewPrivs=1 closes even that path", main.verdict(locked))
        empty = main.parse_status(status_text(uid=1000, prm="0000000000000000", eff="0000000000000000", bnd="0000000000000000", nnp=0))
        self.assertIn("bounding set is empty", main.verdict(empty))
        filecap = main.parse_status(status_text(uid=1000, prm="0000000000000400", eff="0000000000000400", bnd="00000000a80425fb"))
        self.assertEqual(main.verdict(filecap), "uid 1000 holding CAP_NET_BIND_SERVICE: granted by file capabilities or the ambient set")

    def test_explain_renders_sets_no_new_privs_and_seccomp(self):
        text = main.explain(main.parse_status(status_text()))
        self.assertIn("pid 1 (nginx): uid 0, gid 0, groups 0 1 2 3 4 6 10 11 20 26 27", text)
        self.assertIn("effective    0x00000000a80425fb  14 of 41 capabilities: the Docker default set", text)
        self.assertIn("    10  CAP_NET_BIND_SERVICE       bind to ports below 1024", text)
        self.assertIn("permitted    same as effective", text)
        self.assertIn("inheritable  0x0000000000000000  none", text)
        self.assertIn("bounding     0x00000000a80425fb  14 of 41 allowed; 27 can never be gained, not even by a setuid binary", text)
        self.assertIn("ambient      0x0000000000000000  none (a non-root program gains capabilities only from file capabilities)", text)
        self.assertIn("NoNewPrivs   0: execve may add privileges through setuid bits and file capabilities", text)
        self.assertIn("Seccomp      2: filter: a BPF program decides per system call, 1 filter(s)", text)
        self.assertIn("verdict: uid 0 with 14 of 41 capabilities", text)
        hardened = main.explain(main.parse_status(status_text(uid=1000, prm="0000000000000000", eff="0000000000000000", bnd="0000000000000400", nnp=1)))
        self.assertIn("effective    0x0000000000000000  no capabilities: what --cap-drop ALL leaves", hardened)
        self.assertIn("NoNewPrivs   1: execve cannot add privileges; setuid bits and file capabilities are ignored", hardened)
        setuid = main.explain(main.parse_status(status_text(name="passwd").replace("Uid:\t0\t0\t0\t0", "Uid:\t1000\t0\t0\t0")))
        self.assertIn("uid 1000 (effective 0, saved 0)", setuid)

    def test_read_status_from_a_fake_proc_root_or_a_saved_file(self):
        root = tempfile.mkdtemp(prefix="capcheck-proc-")
        self.addCleanup(shutil.rmtree, root)
        os.makedirs(os.path.join(root, "4242"))
        with open(os.path.join(root, "4242", "status"), "w", encoding="utf-8") as handle:
            handle.write(status_text(pid=4242))
        self.assertEqual(main.read_status("4242", root).pid, 4242)
        self.assertEqual(main.read_status(os.path.join(root, "4242", "status")).name, "nginx")
        with self.assertRaises(FileNotFoundError):
            main.read_status("1", root)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="capcheck-cli-")
        self.addCleanup(shutil.rmtree, self.root)
        os.makedirs(os.path.join(self.root, "1"))
        self.status_file = os.path.join(self.root, "1", "status")
        with open(self.status_file, "w", encoding="utf-8") as handle:
            handle.write(status_text())

    def test_decode_json_and_text(self):
        code, out, _ = run_main(["00000000a80425fb", "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual((report["mask"], report["count"]), ("0x00000000a80425fb", 14))
        self.assertEqual(report["names"], DOCKER_DEFAULT_NAMES)
        self.assertEqual(len(report["root_equivalent"]), 8)
        code, out, _ = run_main(["all"])
        self.assertEqual(code, 0)
        self.assertIn("all 41 capabilities", out)

    def test_diff_command(self):
        code, out, _ = run_main(["--diff", "docker-default", "none"])
        self.assertEqual(code, 0)
        self.assertIn("dropped going A -> B (14)", out)
        code, out, _ = run_main(["--diff", "docker-default", "all", "--json"])
        self.assertEqual(len(json.loads(out)["only_b"]), 27)

    def test_perms_and_path_commands(self):
        code, out, _ = run_main(["--perms", "drwxrwxrwt", "--user", "alice", "--groups", "alice", "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual((report["klass"], report["write"], report["traverse"], report["ls"], report["mode"]), ("other", True, True, "drwxrwxrwt", "1777"))
        self.assertTrue(any("sticky" in n for n in report["notes"]))
        code, out, _ = run_main(["--perms", "0600", "--user", "app", "--caps", "docker-default", "--json"])
        self.assertEqual((json.loads(out)["read"], json.loads(out)["caps"][1]), (True, "CAP_DAC_OVERRIDE"))
        code, out, _ = run_main(["--perms", "755", "--dir", "--user", "alice"])
        self.assertIn("drwxr-xr-x root root  (mode 0755, directory) as alice (groups: alice)", out)
        code, out, _ = run_main(["--path", self.status_file, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["owner"], pwd.getpwuid(os.getuid()).pw_name)

    def test_explain_from_a_fake_root_and_from_a_file(self):
        code, out, _ = run_main(["--explain", "1", "--root", self.root])
        self.assertEqual(code, 0)
        self.assertIn("verdict: uid 0 with 14 of 41 capabilities", out)
        code, out, _ = run_main(["--explain", self.status_file, "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["effective"]["names"], DOCKER_DEFAULT_NAMES)
        self.assertEqual(report["bounding"]["mask"], "0x00000000a80425fb")
        self.assertIn("root-equivalent", report["verdict"])

    def test_errors_exit_with_2_for_usage_and_1_for_missing_processes(self):
        code, _, err = run_main(["cap_fly"])
        self.assertEqual(code, 2)
        self.assertIn("unknown capability", err)
        code, _, err = run_main(["--perms", "999"])
        self.assertEqual(code, 2)
        self.assertIn("cannot read mode", err)
        code, _, err = run_main(["--explain", "99999", "--root", self.root])
        self.assertEqual(code, 1)
        self.assertIn("no status file", err)
        code, _, err = run_main(["--explain", "1", "--root", os.path.join(self.root, "nope")])
        self.assertEqual(code, 2)
        self.assertIn("needs Linux procfs", err)


if __name__ == "__main__":
    unittest.main()
