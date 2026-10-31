import copy
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
IDLE = os.path.join(FIXTURES, "idle")
SATURATED = os.path.join(FIXTURES, "saturated")

STAT = """cpu  100 10 50 800 20 5 15 0 0 0
cpu0 60 5 30 400 10 2 8 0 0 0
cpu1 40 5 20 400 10 3 7 0 0 0
intr 12345 1 2 3
ctxt 98765
btime 1793318400
processes 4321
procs_running 3
procs_blocked 1
softirq 777 1 2 3
"""


def snapshots(host):
    return main.sample(os.path.join(host, "t0")), main.sample(os.path.join(host, "t1"))


def make_sample(taken_at, cpu, ncpu=4, running=1, blocked=0, meminfo=None, vmstat=None, pressure=None, disks=None, processes=None):
    cpus = {"cpu": cpu}
    for index in range(ncpu):
        cpus[f"cpu{index}"] = main.CpuTimes()
    meminfo = meminfo or {"MemTotal": 1000000, "MemAvailable": 600000, "MemFree": 300000, "SwapTotal": 0, "SwapFree": 0}
    return main.Sample(taken_at, cpus, running, blocked, 0, (0.5, 0.4, 0.3), meminfo, vmstat or {}, pressure or {}, disks or {}, processes or {})


class ParseTests(unittest.TestCase):
    def test_parse_stat_reads_aggregate_per_cpu_and_counters(self):
        cpus, counters = main.parse_stat(STAT)
        self.assertEqual(sorted(cpus), ["cpu", "cpu0", "cpu1"])
        self.assertEqual((cpus["cpu"].user, cpus["cpu"].idle, cpus["cpu"].iowait, cpus["cpu"].steal), (100, 800, 20, 0))
        self.assertEqual(cpus["cpu"].total, 1000)
        self.assertEqual(counters, {"ctxt": 98765, "btime": 1793318400, "processes": 4321, "procs_running": 3, "procs_blocked": 1})
        with self.assertRaises(ValueError):
            main.parse_stat("intr 1 2 3\nctxt 5\n")

    def test_parse_loadavg_splits_running_and_total(self):
        load, running, total = main.parse_loadavg("12.37 8.44 3.60 14/470 31977\n")
        self.assertEqual((load, running, total), ((12.37, 8.44, 3.60), 14, 470))
        with self.assertRaises(ValueError):
            main.parse_loadavg("0.1 0.2\n")

    def test_parse_meminfo_keeps_kb_values_and_bare_counts(self):
        info = main.parse_meminfo("MemTotal:       16318248 kB\nMemAvailable:   13456780 kB\nHugePages_Total:       0\nGarbage line\nBroken: x kB\n")
        self.assertEqual(info, {"MemTotal": 16318248, "MemAvailable": 13456780, "HugePages_Total": 0})

    def test_parse_pressure_handles_some_only_and_some_plus_full(self):
        both = main.parse_pressure("some avg10=22.40 avg60=14.90 avg300=5.50 total=25756789\nfull avg10=12.10 avg60=7.90 avg300=2.95 total=11086543\n")
        self.assertEqual(both["some"]["avg10"], 22.4)
        self.assertEqual(both["full"]["total"], 11086543.0)
        only_some = main.parse_pressure("some avg10=0.12 avg60=0.08 avg300=0.05 total=1234567\n")
        self.assertEqual(sorted(only_some), ["some"])
        self.assertEqual(main.parse_pressure(""), {})

    def test_parse_diskstats_and_whole_disks_drop_partitions_and_loop_devices(self):
        disks = main.parse_diskstats(main.read_text(os.path.join(SATURATED, "t1", "diskstats")))
        self.assertEqual(sorted(disks), ["loop0", "nvme0n1", "nvme0n1p1", "nvme0n1p2", "sda", "sda1"])
        self.assertEqual((disks["nvme0n1"].reads, disks["nvme0n1"].in_flight, disks["nvme0n1"].ms_weighted), (5253067, 31, 4793000))
        self.assertEqual(sorted(main.whole_disks(disks)), ["nvme0n1", "sda"])

    def test_is_partition_tells_dm_10_from_dm_1_and_sdaa_from_sda(self):
        names = ["dm-1", "dm-10", "sda", "sdaa", "sda1", "nvme0n1", "nvme0n1p2", "md0", "md0p1"]
        self.assertFalse(main.is_partition("dm-10", names))
        self.assertFalse(main.is_partition("sdaa", names))
        self.assertTrue(main.is_partition("sda1", names))
        self.assertTrue(main.is_partition("nvme0n1p2", names))
        self.assertTrue(main.is_partition("md0p1", names))

    def test_parse_pid_stat_survives_spaces_and_parens_in_comm(self):
        proc = main.parse_pid_stat(main.read_text(os.path.join(SATURATED, "t1", "1300", "stat")))
        self.assertEqual((proc.pid, proc.comm, proc.state, proc.utime, proc.stime, proc.threads, proc.rss_pages), (1300, "tmux: server", "S", 4569, 2346, 1, 1234))
        weird = main.parse_pid_stat("77 (a (b) c) R 1 " + " ".join(["0"] * 48) + "\n")
        self.assertEqual((weird.comm, weird.state), ("a (b) c", "R"))
        with self.assertRaises(ValueError):
            main.parse_pid_stat("77 (short) R 1 2 3\n")
        with self.assertRaises(ValueError):
            main.parse_pid_stat("no parens here\n")


class FixtureTests(unittest.TestCase):
    def test_sample_reads_the_whole_snapshot_tree(self):
        before, after = snapshots(SATURATED)
        self.assertEqual((before.taken_at, after.taken_at), (172800.0, 172810.0))
        self.assertEqual(main.ncpus(after), 4)
        self.assertEqual((after.procs_running, after.procs_blocked, after.load), (14, 3, (12.37, 8.44, 3.60)))
        self.assertEqual(sorted(after.pressure), ["cpu", "io", "memory"])
        self.assertEqual(sorted(after.processes), [1, 812, 1204, 1300, 2201, 2250, 28400, 31840, 31841, 31850])
        self.assertEqual(after.meminfo["MemAvailable"], 598112)
        self.assertEqual(after.vmstat["oom_kill"], 3)

    def test_cpu_usage_from_idle_and_saturated_deltas(self):
        idle_before, idle_after = snapshots(IDLE)
        idle = main.cpu_usage(idle_before.cpus["cpu"], idle_after.cpus["cpu"])
        self.assertAlmostEqual(idle.busy, 4.05, places=2)
        self.assertEqual((idle.steal, idle.iowait), (0.0, 0.27))
        before, after = snapshots(SATURATED)
        busy = main.cpu_usage(before.cpus["cpu"], after.cpus["cpu"])
        self.assertEqual(busy.busy, 97.88)
        self.assertEqual((busy.user, busy.system, busy.iowait, busy.steal, busy.idle, busy.irq), (86.5, 9.25, 1.75, 1.62, 0.38, 0.5))
        self.assertAlmostEqual(main.cpu_usage(after.cpus["cpu0"], after.cpus["cpu0"]).idle, 100.0)

    def test_memory_view_uses_available_not_free_and_counts_swap_and_oom(self):
        before, after = snapshots(SATURATED)
        mem = main.memory_view(before, after, 10.0)
        self.assertEqual((mem.total_kb, mem.available_kb, mem.available_pct), (16318248, 598112, 3.67))
        self.assertEqual(mem.swap_used_kb, 4194300 - 2321678)
        self.assertEqual((mem.swap_in_kb_per_s, mem.swap_out_kb_per_s, mem.major_faults_per_s), (2000.0, 2400.0, 450.0))
        self.assertEqual((mem.oom_kills, mem.oom_kills_total), (1, 3))

    def test_pressure_view_reports_avg10_and_the_exact_share_of_this_window(self):
        before, after = snapshots(SATURATED)
        cpu = main.pressure_view("cpu", before, after, 10.0)
        self.assertEqual((cpu.some_avg10, cpu.full_avg10, cpu.some_window_pct, cpu.full_window_pct), (38.21, 0.0, 41.0, 0.0))
        memory = main.pressure_view("memory", before, after, 10.0)
        self.assertEqual((memory.full_avg10, memory.full_window_pct), (12.1, 12.1))
        self.assertIsNone(main.pressure_view("cpu", before, make_sample(1.0, main.CpuTimes()), 1.0))

    def test_disk_usage_turns_counters_into_iops_throughput_busy_and_queue(self):
        before, after = snapshots(SATURATED)
        nvme = main.disk_usage("nvme0n1", before.disks["nvme0n1"], after.disks["nvme0n1"], 10.0)
        self.assertEqual((nvme.reads_per_s, nvme.writes_per_s), (1850.0, 620.0))
        self.assertEqual((nvme.read_kb_per_s, nvme.write_kb_per_s), (148000.0, 49600.0))
        self.assertEqual((nvme.busy_pct, nvme.queue, nvme.await_ms, nvme.in_flight), (98.7, 9.3, 3.77, 31))
        sda = main.disk_usage("sda", before.disks["sda"], after.disks["sda"], 10.0)
        self.assertEqual((sda.busy_pct, sda.await_ms), (0.0, 0.0))

    def test_top_processes_name_the_busy_loops_and_d_state_lists_the_blocked_ones(self):
        before, after = snapshots(SATURATED)
        usages = main.process_usage(before, after, 10.0)
        top = main.top_processes(usages, 3)
        self.assertEqual([(p.pid, p.comm, p.cpu_pct) for p in top], [(31840, "python3", 99.0), (31841, "python3", 97.8), (31850, "dd", 65.5)])
        self.assertEqual([(p.pid, p.comm) for p in main.blocked_processes(usages)], [(2250, "postgres"), (31850, "dd")])
        self.assertEqual(top[0].rss_mb, 10.0)

    def test_verdicts_for_the_idle_and_the_saturated_host(self):
        idle = main.analyse(*snapshots(IDLE))
        self.assertEqual(idle.verdicts, ["No saturation: CPU 4%, memory 82% available, busiest disk nvme0n1 3%"])
        saturated = main.analyse(*snapshots(SATURATED))
        self.assertEqual(saturated.verdicts[0], "CPU saturated: run queue 14 on 4 cpus, PSI cpu some 38%")
        self.assertEqual(saturated.verdicts[1], "Memory saturated: 4% available, PSI memory full 12%, swap in/out 2000/2400 kB/s, 1 OOM kill(s)")
        self.assertEqual(saturated.verdicts[2], "Disk saturated: nvme0n1 99% busy, queue 9.3, await 3.8 ms, PSI io full 9%")
        self.assertEqual(saturated.verdicts[3], "2 process(es) in D state waiting on I/O: postgres (2250), dd (31850)")
        self.assertEqual((saturated.elapsed, saturated.ncpu, saturated.run_queue, saturated.context_switches_per_s), (10.0, 4, 14, 18500.0))


class VerdictTests(unittest.TestCase):
    def test_steal_is_called_out_and_busy_is_not_saturated_without_a_queue(self):
        before = make_sample(0.0, main.CpuTimes(idle=1000))
        after = make_sample(10.0, main.CpuTimes(user=500, steal=300, idle=1200))
        report = main.analyse(before, after)
        self.assertEqual(report.cpu.steal, 30.0)
        self.assertEqual(report.verdicts[0], "CPU busy, not saturated: 80% utilization, run queue 1 on 4 cpus")
        self.assertTrue(report.verdicts[1].startswith("CPU steal 30%"))

    def test_psi_alone_can_declare_cpu_and_disk_saturated(self):
        pressure = {"cpu": {"some": {"avg10": 25.0, "total": 2500000.0}}, "io": {"some": {"avg10": 9.0, "total": 0.0}, "full": {"avg10": 7.0, "total": 0.0}}}
        before = make_sample(0.0, main.CpuTimes(idle=1000), disks={"vda": main.DiskTimes()})
        after = make_sample(10.0, main.CpuTimes(user=200, idle=1800), pressure=pressure, disks={"vda": main.DiskTimes(reads=10, ms_busy=500, ms_weighted=800)})
        report = main.analyse(before, after)
        self.assertEqual(report.verdicts, ["CPU saturated: run queue 1 on 4 cpus, PSI cpu some 25%", "Disk saturated: vda 5% busy, queue 0.1, await 0.0 ms, PSI io full 7%"])
        self.assertEqual(report.pressure["cpu"].some_window_pct, 25.0)

    def test_oom_kills_without_host_pressure_point_at_a_cgroup_limit(self):
        before = make_sample(0.0, main.CpuTimes(idle=1000), vmstat={"oom_kill": 4})
        after = make_sample(5.0, main.CpuTimes(idle=1500), vmstat={"oom_kill": 6})
        report = main.analyse(before, after)
        self.assertEqual(report.verdicts, ["Memory errors: oom_kill rose by 2 with 60% available; a cgroup limit ran out, not the host"])
        self.assertEqual(report.rows[1].errors, "oom_kill +2 (total 6)")

    def test_low_available_memory_is_saturation_even_without_psi(self):
        meminfo = {"MemTotal": 1000000, "MemAvailable": 50000, "MemFree": 10000, "SwapTotal": 200000, "SwapFree": 100000}
        before = make_sample(0.0, main.CpuTimes(idle=1000), vmstat={"pswpout": 0})
        after = make_sample(10.0, main.CpuTimes(idle=1500), meminfo=meminfo, vmstat={"pswpout": 1000})
        report = main.analyse(before, after)
        self.assertEqual(report.verdicts, ["Memory saturated: 5% available, swap in/out 0/400 kB/s"])
        self.assertIn("PSI: not available", main.render(report))

    def test_processes_without_a_first_sample_are_skipped(self):
        newcomer = main.ProcTimes(99, "burst", "R", 900, 0, 1, 10)
        before = make_sample(0.0, main.CpuTimes(idle=1000), processes={1: main.ProcTimes(1, "init", "S", 10, 10, 1, 100)})
        after = make_sample(10.0, main.CpuTimes(idle=1500), processes={1: main.ProcTimes(1, "init", "S", 30, 20, 1, 100), 99: newcomer})
        usages = main.process_usage(before, after, 10.0)
        self.assertEqual([(u.pid, u.cpu_pct) for u in usages], [(1, 3.0)])


class RenderAndCliTests(unittest.TestCase):
    def test_render_prints_the_use_table_psi_top_and_verdict(self):
        text = main.render(main.analyse(*snapshots(SATURATED)))
        self.assertIn("usetop: 10.0s window, 4 cpus, load 12.37 8.44 3.60, 18500 context switches/s", text)
        self.assertIn("Resource       Utilization", text)
        self.assertRegex(text, r"CPU\s+98% \(us 86 sy 9 wa 2 st 2\)\s+run queue 14 on 4 cpus, load 12.37; PSI some 38.2%")
        self.assertRegex(text, r"Memory\s+96% used, 4% available of 15935 MB\s+swap in 2000 out 2400 kB/s")
        self.assertRegex(text, r"Disk nvme0n1\s+99% busy \(1850 r/s 620 w/s, 193.0 MB/s\)\s+queue 9.3, await 3.8 ms, 31 in flight; PSI io full 8.7%; 2 in D state")
        self.assertIn("PSI avg10:  cpu some 38.2% full 0.0% (this window: 41.0% / 0.0%)", text)
        self.assertIn("31840 python3          R   99.0%", text)
        self.assertIn("D state (uninterruptible, waiting on I/O):", text)
        self.assertTrue(text.splitlines()[-1].startswith("VERDICT: CPU saturated: run queue 14 on 4 cpus, PSI cpu some 38% | Memory saturated"))

    def test_main_replay_emits_one_json_document_per_pair(self):
        out = StringIO()
        with redirect_stdout(out):
            code = main.main(["--replay", SATURATED, "--json", "--top", "2"])
        self.assertEqual(code, 0)
        documents = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(documents), 1)
        report = documents[0]
        self.assertEqual((report["ncpu"], report["run_queue"], report["cpu"]["busy"], report["memory"]["available_pct"]), (4, 14, 97.88, 3.67))
        self.assertEqual([p["comm"] for p in report["top"]], ["python3", "python3"])
        self.assertEqual(report["disks"]["nvme0n1"]["busy_pct"], 98.7)
        self.assertEqual(report["rows"][0]["resource"], "CPU")
        self.assertEqual(len(report["verdicts"]), 4)
        self.assertEqual(report["pressure"]["io"]["full_window_pct"], 8.7)

    def test_main_fails_cleanly_without_procfs_and_on_bad_replay_dirs(self):
        err = StringIO()
        with tempfile.TemporaryDirectory() as empty, redirect_stderr(err):
            self.assertEqual(main.main(["--root", os.path.join(empty, "nope")]), 2)
            self.assertEqual(main.main(["--replay", empty]), 1)
            self.assertEqual(main.main(["--replay", FIXTURES]), 1)
        self.assertIn("needs Linux procfs", err.getvalue())
        self.assertIn("need at least two snapshot directories", err.getvalue())
        self.assertIn("is not a /proc snapshot", err.getvalue())

    def test_watch_takes_count_plus_one_samples_with_a_fake_clock(self):
        idle_before, idle_after = snapshots(IDLE)
        later = copy.deepcopy(idle_after)
        later.taken_at += 5.0
        later.cpus["cpu"].idle += 2000
        queue = iter([idle_before, idle_after, later])
        slept, emitted = [], []
        reports = main.watch(lambda: next(queue), 2.0, 2, sleep=slept.append, emit=emitted.append, as_json=True)
        self.assertEqual(slept, [2.0, 2.0])
        self.assertEqual([r.elapsed for r in reports], [10.0, 5.0])
        self.assertEqual(len(emitted), 2)
        self.assertEqual(json.loads(emitted[1])["cpu"]["idle"], 100.0)


if __name__ == "__main__":
    unittest.main()
