import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

CPU_STAT_THROTTLED = "usage_usec 412345\nuser_usec 400000\nsystem_usec 12345\nnr_periods 20\nnr_throttled 20\nthrottled_usec 1600000\nnr_bursts 0\nburst_usec 0\n"
CPU_STAT_IDLE = "usage_usec 1000\nuser_usec 800\nsystem_usec 200\nnr_periods 0\nnr_throttled 0\nthrottled_usec 0\n"
MEMORY_EVENTS_CLEAN = "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n"


def write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def make_group(root, name, files=None, procs="", subtree_control="", controllers="cpu memory pids", populated=None):
    path = os.path.join(root, *[part for part in name.split("/") if part])
    os.makedirs(path, exist_ok=True)
    write(os.path.join(path, "cgroup.procs"), procs)
    write(os.path.join(path, "cgroup.controllers"), controllers)
    write(os.path.join(path, "cgroup.subtree_control"), subtree_control)
    if populated is not None:
        write(os.path.join(path, "cgroup.events"), f"populated {int(populated)}\nfrozen 0\n")
    for file_name, text in (files or {}).items():
        write(os.path.join(path, file_name), text)
    return path


def make_root(tmp, subtree_control="cpu memory pids", controllers="cpuset cpu io memory pids"):
    root = os.path.join(tmp, "cgroup")
    make_group(root, "", procs="1\n2\n", subtree_control=subtree_control, controllers=controllers)
    return root


def make_proc(tmp, pid, cgroup_text, comm="node"):
    proc = os.path.join(tmp, "proc")
    os.makedirs(os.path.join(proc, str(pid)), exist_ok=True)
    write(os.path.join(proc, str(pid), "cgroup"), cgroup_text)
    write(os.path.join(proc, str(pid), "comm"), comm + "\n")
    return proc


class FakeTree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cgtool-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = make_root(self.tmp)

    def throttled_group(self, name="demo"):
        return make_group(
            self.root,
            name,
            {
                "cpu.max": "20000 100000\n",
                "cpu.weight": "100\n",
                "cpu.stat": CPU_STAT_THROTTLED,
                "memory.current": "18874368\n",
                "memory.peak": "20971520\n",
                "memory.max": "max\n",
                "memory.high": "max\n",
                "memory.swap.current": "0\n",
                "memory.swap.max": "0\n",
                "memory.events": MEMORY_EVENTS_CLEAN,
                "memory.oom.group": "0\n",
                "pids.current": "1\n",
                "pids.max": "max\n",
                "pids.events": "max 0\n",
            },
            procs="4242\n",
            populated=True,
        )


class ParsingTests(unittest.TestCase):
    def test_sizes_and_memory_setting(self):
        self.assertEqual(main.parse_size("200M"), 209715200)
        self.assertEqual(main.parse_size("1G"), 1073741824)
        self.assertEqual(main.parse_size("512k"), 524288)
        self.assertEqual(main.parse_size("4096"), 4096)
        self.assertEqual(main.memory_setting("30M"), "31457280")
        self.assertEqual(main.memory_setting("max"), "max")
        with self.assertRaises(ValueError):
            main.parse_size("lots")
        with self.assertRaises(ValueError):
            main.pids_setting("0")

    def test_cpu_quota_period_millicores_and_weight(self):
        self.assertEqual(main.cpu_max_setting("0.5"), "50000 100000")
        self.assertEqual(main.cpu_max_setting("2"), "200000 100000")
        self.assertEqual(main.cpu_max_setting("0.2"), "20000 100000")
        self.assertEqual(main.cpu_max_setting("max"), "max 100000")
        for bad in ("0", "-1", "half"):
            with self.assertRaises(ValueError):
                main.cpu_max_setting(bad)
        self.assertEqual(main.parse_cpu_max("50000 100000"), (50000, 100000))
        self.assertEqual(main.parse_cpu_max("max 100000"), (None, 100000))
        self.assertEqual(main.cpus_from_cpu_max("25000 50000"), 0.5)
        self.assertIsNone(main.cpus_from_cpu_max("max 100000"))
        self.assertEqual(main.millicores_to_cpu_max(500), "50000 100000")
        self.assertEqual(main.millicores_to_cpu_max(1500), "150000 100000")
        self.assertEqual(main.shares_to_weight(2), 1)
        self.assertEqual(main.shares_to_weight(1024), 39)
        self.assertEqual(main.shares_to_weight(262144), 10000)

    def test_key_value_files_limits_and_proc_cgroup(self):
        stat = main.parse_kv(CPU_STAT_THROTTLED)
        self.assertEqual((stat["usage_usec"], stat["nr_throttled"], stat["throttled_usec"]), (412345, 20, 1600000))
        self.assertEqual(main.parse_kv(None), {})
        self.assertIsNone(main.parse_limit("max\n"))
        self.assertEqual(main.parse_limit("209715200\n"), 209715200)
        self.assertEqual(main.parse_limit("50000 100000"), 50000)
        self.assertEqual(main.parse_proc_cgroup("0::/docker/abc123\n"), ("/docker/abc123", []))
        path, v1 = main.parse_proc_cgroup("12:memory:/docker/abc\n3:cpu,cpuacct:/docker/abc\n0::/docker/abc\n")
        self.assertEqual(path, "/docker/abc")
        self.assertEqual(len(v1), 2)
        self.assertEqual(main.parse_proc_cgroup("garbage\n"), (None, []))
        self.assertEqual(main.human_size(209715200), "200.0M")
        self.assertEqual(main.human_size(None), "max")


class PlanTests(FakeTree):
    def test_plan_writes_the_four_limit_files_under_an_enabled_parent(self):
        plan = main.plan_create(self.root, "demo", main.Limits("0.5", "200M", "50"))
        self.assertEqual(plan.problems, [])
        self.assertEqual(plan.mkdirs, [os.path.join(self.root, "demo")])
        self.assertEqual(plan.enables, [])
        group = os.path.join(self.root, "demo")
        self.assertEqual(
            plan.writes,
            [
                (os.path.join(group, "cpu.max"), "50000 100000"),
                (os.path.join(group, "memory.max"), "209715200"),
                (os.path.join(group, "memory.swap.max"), "0"),
                (os.path.join(group, "pids.max"), "50"),
            ],
        )
        text = main.render_plan(self.root, plan)
        self.assertIn("group /demo", text)
        self.assertIn("write '50000 100000' to", text)

    def test_plan_enables_missing_controllers_down_the_chain(self):
        write(os.path.join(self.root, "cgroup.subtree_control"), "memory pids")
        plan = main.plan_create(self.root, "team/web", main.Limits("1", "100M", "10"))
        self.assertEqual(plan.problems, [])
        self.assertEqual(plan.mkdirs, [os.path.join(self.root, "team"), os.path.join(self.root, "team", "web")])
        self.assertEqual(
            plan.enables,
            [
                (os.path.join(self.root, "cgroup.subtree_control"), "+cpu"),
                (os.path.join(self.root, "team", "cgroup.subtree_control"), "+cpu +memory +pids"),
            ],
        )
        text = main.render_plan(self.root, plan)
        self.assertIn(f"echo +cpu > {os.path.join(self.root, 'cgroup.subtree_control')}", text)
        self.assertIn("echo +cpu +memory +pids >", text)

    def test_plan_refuses_a_parent_that_holds_processes(self):
        make_group(self.root, "busy", procs="4242\n4243\n")
        plan = main.plan_create(self.root, "busy/child", main.Limits(cpu="0.5"))
        self.assertEqual(len(plan.problems), 1)
        self.assertIn("no-internal-processes rule", plan.problems[0])
        self.assertIn("/busy", plan.problems[0])
        self.assertIn("2 process(es)", plan.problems[0])
        self.assertEqual(plan.writes, [])

    def test_plan_reports_a_controller_the_kernel_does_not_offer_and_the_root(self):
        write(os.path.join(self.root, "cgroup.controllers"), "memory pids")
        plan = main.plan_create(self.root, "demo", main.Limits(cpu="0.5"))
        self.assertIn("controller cpu is not available", plan.problems[0])
        self.assertEqual(main.plan_create(self.root, "/", main.Limits(cpu="0.5")).problems, ["refusing to change the root cgroup; give the group a name"])
        with self.assertRaises(ValueError):
            main.group_path(self.root, "../etc")

    def test_apply_plan_creates_writes_and_skips_swap_file_without_swap_accounting(self):
        plan = main.plan_create(self.root, "demo", main.Limits("0.5", "200M", "50"))
        logged = []
        self.assertEqual(main.apply_plan(plan, logged.append), [])
        group = os.path.join(self.root, "demo")
        self.assertEqual(main.read_text(os.path.join(group, "cpu.max")), "50000 100000")
        self.assertEqual(main.read_text(os.path.join(group, "memory.max")), "209715200")
        self.assertEqual(main.read_text(os.path.join(group, "pids.max")), "50")
        self.assertFalse(os.path.exists(os.path.join(group, "memory.swap.max")))
        self.assertTrue(any("swap accounting is off" in line for line in logged))
        make_group(self.root, "swapped", {"memory.swap.max": "max\n"})
        plan2 = main.plan_create(self.root, "swapped", main.Limits(memory="30M"))
        self.assertEqual(main.apply_plan(plan2), [])
        self.assertEqual(main.read_text(os.path.join(self.root, "swapped", "memory.swap.max")), "0")
        write(os.path.join(self.root, "cgroup.subtree_control"), "memory pids")
        plan3 = main.plan_create(self.root, "team/web", main.Limits(cpu="1"))
        self.assertEqual(main.apply_plan(plan3), [])
        self.assertEqual(main.read_text(os.path.join(self.root, "cgroup.subtree_control")), "+cpu")
        self.assertEqual(main.read_text(os.path.join(self.root, "team", "cgroup.subtree_control")), "+cpu")


class StatTests(FakeTree):
    def test_read_group_and_throttling_verdict(self):
        self.throttled_group()
        stats = main.read_group(self.root, "demo", now=lambda: 1000.0)
        self.assertEqual((stats.name, stats.procs, stats.populated), ("/demo", [4242], True))
        self.assertEqual((stats.cpu_max, stats.cpu_weight), ("20000 100000", 100))
        self.assertEqual((stats.memory_current, stats.memory_peak, stats.memory_max), (18874368, 20971520, "max"))
        self.assertEqual(stats.pids_events, {"max": 0})
        lines = main.verdicts(stats)
        self.assertEqual(len(lines), 1)
        self.assertIn("CPU: throttled in 100% of 20 periods, 1.60s spent waiting for quota (cpu.max 20000 100000)", lines[0])
        text = main.render_stat(stats)
        self.assertIn("cgroup /demo  procs 1  controllers cpu memory pids", text)
        self.assertIn("usage 0.412s  periods 20  throttled 20  throttled_usec 1600000", text)
        self.assertIn("-> CPU: throttled", text)
        data = main.stats_to_json(stats)
        self.assertEqual(data["cpus"], 0.2)
        self.assertEqual(data["verdicts"], lines)

    def test_oom_kill_swap_and_pids_verdicts(self):
        make_group(
            self.root,
            "mem",
            {
                "cpu.max": "max 100000\n",
                "cpu.stat": CPU_STAT_IDLE,
                "memory.current": "0\n",
                "memory.max": "31457280\n",
                "memory.high": "max\n",
                "memory.swap.max": "max\n",
                "memory.events": "low 0\nhigh 0\nmax 7\noom 1\noom_kill 1\noom_group_kill 0\n",
                "pids.current": "5\n",
                "pids.max": "5\n",
                "pids.events": "max 15\n",
            },
            populated=False,
        )
        lines = main.verdicts(main.read_group(self.root, "mem"))
        self.assertTrue(any(line.startswith("memory: 1 OOM kill(s) at memory.max 30.0M") and "137" in line for line in lines), lines)
        self.assertTrue(any("memory.swap.max is max" in line and "set it to 0" in line for line in lines), lines)
        self.assertTrue(any(line == "pids: fork() failed 15 time(s) with EAGAIN at pids.max 5" for line in lines), lines)
        self.assertIn("no processes in this group right now; counters are history", lines)
        self.assertFalse(any(line.startswith("CPU: quota") for line in lines))

    def test_near_limit_memory_and_pids_at_limit(self):
        make_group(
            self.root,
            "tight",
            {
                "cpu.max": "50000 100000\n",
                "cpu.stat": CPU_STAT_IDLE,
                "memory.current": "199000000\n",
                "memory.max": "209715200\n",
                "memory.high": "150000000\n",
                "memory.swap.max": "0\n",
                "memory.events": "low 0\nhigh 3\nmax 0\noom 0\noom_kill 0\n",
                "pids.current": "50\n",
                "pids.max": "50\n",
                "pids.events": "max 0\n",
            },
        )
        lines = main.verdicts(main.read_group(self.root, "tight"))
        self.assertIn("CPU: quota of 0.5 CPU(s) never hit", lines)
        self.assertTrue(any("memory.high 143.1M crossed 3 time(s)" in line for line in lines), lines)
        self.assertTrue(any(line.startswith("memory: 189.8M of 200.0M in use (95%)") for line in lines), lines)
        self.assertIn("pids: at the limit (50/50); the next fork() returns EAGAIN", lines)

    def test_watch_diffs_two_samples_with_fake_sleep(self):
        self.throttled_group()
        first = main.read_group(self.root, "demo", now=lambda: 100.0)
        second = main.read_group(self.root, "demo", now=lambda: 102.0)
        second.cpu_stat = dict(second.cpu_stat, usage_usec=412345 + 400000, nr_throttled=36, throttled_usec=1600000 + 1600000)
        second.memory_current = 18874368 + 1048576
        second.memory_events = dict(second.memory_events, oom_kill=1)
        samples = iter([first, second])
        slept, emitted = [], []
        deltas = main.watch_group(lambda: next(samples), 2.0, 1, sleep=slept.append, emit=emitted.append)
        self.assertEqual(slept, [2.0])
        self.assertEqual(len(deltas), 1)
        self.assertAlmostEqual(deltas[0]["cpu_percent"], 20.0)
        self.assertEqual((deltas[0]["nr_throttled"], deltas[0]["throttled_usec"], deltas[0]["oom_kill"]), (16, 1600000, 1))
        self.assertEqual(deltas[0]["memory_delta"], 1048576)
        self.assertTrue(emitted[0].startswith("cgroup /demo"))
        self.assertIn("2.0s: cpu 20%  throttled 16 period(s) / 1.60s  memory 19.0M (+1.0M)  pids 1  OOM kills +1", emitted[1])


class RunTests(FakeTree):
    def test_plan_run_requires_an_existing_leaf_group(self):
        self.assertIn("create it first: cgtool create demo --cpu 0.5", main.plan_run(self.root, "demo")[0])
        make_group(self.root, "parent", subtree_control="cpu memory")
        self.assertIn("cannot hold processes itself; run in a leaf such as parent/leaf", main.plan_run(self.root, "parent")[0])
        make_group(self.root, "leaf")
        self.assertEqual(main.plan_run(self.root, "leaf"), [])

    def test_run_in_group_attaches_the_child_before_exec_and_reports_an_oom_kill(self):
        group = make_group(self.root, "mem", {"memory.max": "31457280\n", "memory.events": MEMORY_EVENTS_CLEAN})
        seen = {}

        def fake_spawn(command, before_exec):
            seen["command"] = list(command)
            before_exec()
            seen["procs"] = main.read_text(os.path.join(group, "cgroup.procs"))
            write(os.path.join(group, "memory.events"), MEMORY_EVENTS_CLEAN.replace("oom_kill 0", "oom_kill 1"))
            return 777

        logged = []
        code = main.run_in_group(self.root, "mem", ["python3", "-c", "x"], spawn=fake_spawn, waitpid=lambda pid, flags: (pid, 9), log=logged.append)
        self.assertEqual(code, 137)
        self.assertEqual(seen["command"], ["python3", "-c", "x"])
        self.assertEqual(seen["procs"], str(os.getpid()))
        self.assertEqual(logged, ["exit 137: the kernel OOM-killed 1 process(es) in /mem (memory.events oom_kill +1, memory.max 30.0M)"])
        self.assertEqual(main.run_in_group(self.root, "mem", ["true"], spawn=lambda c, b: 1, waitpid=lambda p, f: (p, 3 << 8), log=logged.append), 3)
        self.assertEqual(logged[-1], "exit 3")
        self.assertEqual(main.run_in_group(self.root, "missing", ["true"], log=logged.append), 2)
        self.assertEqual(main.exit_code_from_status(15), 143)
        self.assertEqual(main.exit_code_from_status(0), 0)


class ExplainTests(FakeTree):
    def kube_tree(self):
        make_group(self.root, "kubepods", {"cpu.max": "max 100000\n", "cpu.weight": "100\n", "memory.max": "max\n", "pids.max": "max\n", "memory.current": "734003200\n"}, subtree_control="cpu memory pids")
        make_group(self.root, "kubepods/burstable", {"cpu.max": "max 100000\n", "cpu.weight": "100\n", "memory.max": "max\n", "pids.max": "max\n", "memory.current": "314572800\n"}, subtree_control="cpu memory pids")
        make_group(self.root, "kubepods/burstable/pod1", {"cpu.max": "max 100000\n", "cpu.weight": "39\n", "memory.max": "314572800\n", "pids.max": "max\n", "memory.current": "150000000\n"}, subtree_control="cpu memory pids")
        make_group(self.root, "kubepods/burstable/pod1/ctr", {"cpu.max": "50000 100000\n", "cpu.weight": "20\n", "memory.max": "209715200\n", "memory.high": "max\n", "memory.swap.max": "0\n", "pids.max": "max\n", "memory.current": "149000000\n"}, procs="4242\n")

    def test_explain_walks_the_chain_and_picks_the_tightest_limit_per_resource(self):
        self.kube_tree()
        proc = make_proc(self.tmp, 4242, "0::/kubepods/burstable/pod1/ctr\n")
        exp = main.explain_pid(4242, proc, self.root)
        self.assertEqual((exp.pid, exp.comm, exp.path), (4242, "node", "/kubepods/burstable/pod1/ctr"))
        self.assertEqual([level.name for level in exp.levels], ["/", "/kubepods", "/kubepods/burstable", "/kubepods/burstable/pod1", "/kubepods/burstable/pod1/ctr"])
        self.assertEqual(exp.levels[-1].cpu_weight, 20)
        self.assertEqual(exp.effective["cpu_max"], "50000 100000 (0.5 CPU, set at /kubepods/burstable/pod1/ctr)")
        self.assertEqual(exp.effective["memory_max"], "200.0M (set at /kubepods/burstable/pod1/ctr)")
        self.assertEqual(exp.effective["pids_max"], "max (no pids ceiling anywhere in the chain)")
        self.assertEqual(exp.notes, [])
        text = main.render_explain(exp)
        self.assertIn("pid 4242 (node) is in cgroup /kubepods/burstable/pod1/ctr", text)
        self.assertIn("effective memory_max: 200.0M", text)

    def test_tightest_limit_can_sit_on_an_ancestor(self):
        self.kube_tree()
        write(os.path.join(self.root, "kubepods", "burstable", "pod1", "ctr", "memory.max"), "max\n")
        write(os.path.join(self.root, "kubepods", "pids.max"), "4000\n")
        exp = main.explain_pid(4242, make_proc(self.tmp, 4242, "0::/kubepods/burstable/pod1/ctr\n"), self.root)
        self.assertEqual(exp.effective["memory_max"], "300.0M (set at /kubepods/burstable/pod1)")
        self.assertEqual(exp.effective["pids_max"], "4000 (set at /kubepods)")

    def test_hybrid_v1_lines_and_invisible_paths_are_noted(self):
        proc = make_proc(self.tmp, 7, "12:memory:/docker/abc\n0::/docker/abc\n", comm="sh")
        exp = main.explain_pid(7, proc, self.root)
        self.assertEqual([level.name for level in exp.levels], ["/"])
        self.assertTrue(any("cgroup v1 (legacy)" in note for note in exp.notes), exp.notes)
        self.assertTrue(any("cgroup namespace" in note for note in exp.notes), exp.notes)
        with self.assertRaises(FileNotFoundError):
            main.explain_pid(99999, proc, self.root)


class TreeTests(FakeTree):
    def busy_tree(self, usage):
        for name, memory, pids, cpu in (("a", 104857600, 3, usage), ("b", 52428800, 1, 1000), ("b/c", 10485760, 2, 500)):
            make_group(self.root, name, {"cpu.stat": f"usage_usec {cpu}\nnr_periods 10\nnr_throttled {5 if name == 'a' else 0}\nthrottled_usec 0\n", "memory.current": f"{memory}\n", "pids.current": f"{pids}\n"})
        os.makedirs(os.path.join(self.root, "a", "not-a-cgroup"))

    def test_walk_depth_and_top_sorting_with_two_samples(self):
        self.busy_tree(1000000)
        self.assertEqual(main.walk_groups(self.root), ["/", "/a", "/b", "/b/c"])
        self.assertEqual(main.walk_groups(self.root, depth=1), ["/", "/a", "/b"])
        before = main.sample_tree(self.root, now=lambda: 10.0)
        write(os.path.join(self.root, "a", "cpu.stat"), "usage_usec 1800000\nnr_periods 30\nnr_throttled 15\nthrottled_usec 0\n")
        after = main.sample_tree(self.root, now=lambda: 12.0)
        rows = main.top_rows(before, after, sort="cpu", limit=2)
        self.assertEqual([row.name for row in rows], ["/a", "/"])
        self.assertAlmostEqual(rows[0].cpu_percent, 40.0)
        self.assertAlmostEqual(rows[0].throttled_percent, 50.0)
        by_memory = main.top_rows(after, after, sort="memory", limit=10)
        self.assertEqual([row.name for row in by_memory][:3], ["/a", "/b", "/b/c"])
        self.assertIsNone(by_memory[0].cpu_percent)
        self.assertEqual([row.name for row in main.top_rows(after, after, sort="pids")][:1], ["/a"])
        text = main.render_top(rows)
        self.assertIn("cgroup", text.splitlines()[0])
        self.assertIn("/a", text.splitlines()[1])
        with self.assertRaises(ValueError):
            main.top_rows(after, after, sort="io")

    def test_delete_kills_then_removes_deepest_first(self):
        self.busy_tree(0)
        killed, removed = [], []

        def rmdir(path):
            removed.append(path)
            shutil.rmtree(path)

        result = main.delete_group(self.root, "b", kill=killed.append, rmdir=rmdir, sleep=lambda s: None)
        self.assertEqual(result, [os.path.join(self.root, "b", "c"), os.path.join(self.root, "b")])
        self.assertEqual(killed, result)
        self.assertEqual(removed, result)
        self.assertFalse(os.path.exists(os.path.join(self.root, "b")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "a")))
        with self.assertRaises(ValueError):
            main.delete_group(self.root, "/")
        with self.assertRaises(FileNotFoundError):
            main.delete_group(self.root, "gone")


class CliTests(FakeTree):
    def run_main(self, argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_stat_explain_and_dry_run_through_the_cli(self):
        self.throttled_group()
        code, out, _ = self.run_main(["--root", self.root, "stat", "demo", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual((data["name"], data["cpus"], data["cpu_stat"]["nr_throttled"]), ("/demo", 0.2, 20))
        proc = make_proc(self.tmp, 4242, "0::/demo\n", comm="python3")
        code, out, _ = self.run_main(["--root", self.root, "--proc", proc, "explain", "4242"])
        self.assertEqual(code, 0)
        self.assertIn("pid 4242 (python3) is in cgroup /demo", out)
        self.assertIn("effective cpu_max: 20000 100000 (0.2 CPU, set at /demo)", out)
        code, out, _ = self.run_main(["--root", self.root, "create", "fresh", "--cpu", "0.5", "--memory", "200M", "--pids", "50", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("write '209715200' to", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "fresh")))
        code, out, _ = self.run_main(["--root", self.root, "create", "fresh", "--cpu", "0.5"])
        self.assertEqual(code, 0)
        self.assertEqual(main.read_text(os.path.join(self.root, "fresh", "cpu.max")), "50000 100000")
        code, out, _ = self.run_main(["--root", self.root, "top", "--limit", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 3)

    def test_errors_are_explained_with_exit_codes(self):
        code, _, err = self.run_main(["--root", os.path.join(self.tmp, "nope"), "stat", "x"])
        self.assertEqual(code, 2)
        self.assertIn("not found", err)
        v1 = os.path.join(self.tmp, "v1")
        os.makedirs(os.path.join(v1, "memory"))
        code, _, err = self.run_main(["--root", v1, "stat", "x"])
        self.assertEqual(code, 2)
        self.assertIn("cgroup v1 (legacy)", err)
        code, _, err = self.run_main(["--root", self.root, "create", "x"])
        self.assertEqual(code, 2)
        self.assertIn("nothing to set", err)
        code, _, err = self.run_main(["--root", self.root, "stat", "missing"])
        self.assertEqual(code, 1)
        self.assertIn("no cgroup /missing", err)
        code, _, err = self.run_main(["--root", self.root, "create", "x", "--cpu", "0"])
        self.assertEqual(code, 1)
        self.assertIn("cpu limit must be above 0", err)
        make_group(self.root, "busy", procs="99\n")
        code, out, _ = self.run_main(["--root", self.root, "create", "busy/child", "--pids", "5"])
        self.assertEqual(code, 1)
        self.assertIn("problem: cannot enable pids in /busy", out)
        code, _, err = self.run_main(["--root", self.root, "run", "x"])
        self.assertEqual(code, 2)
        self.assertIn("pass a command after --", err)


if __name__ == "__main__":
    unittest.main()
