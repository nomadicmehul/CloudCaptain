import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def load(name):
    return main.parse(fixture(name), name)


class LineParsingTests(unittest.TestCase):
    def test_plain_call_with_errno_text_and_duration(self):
        trace = main.parse('openat(AT_FDCWD, "/etc/app.yml", O_RDONLY|O_CLOEXEC) = -1 ENOENT (No such file or directory) <0.000011>')
        call = trace.calls[0]
        self.assertEqual((call.name, call.result, call.errno, call.error_text, call.duration), ("openat", -1, "ENOENT", "No such file or directory", 0.000011))
        self.assertEqual(call.args, ["AT_FDCWD", '"/etc/app.yml"', "O_RDONLY|O_CLOEXEC"])
        self.assertEqual(call.target, "/etc/app.yml")
        self.assertIsNone(call.pid)
        self.assertTrue(call.failed)

    def test_prefix_variants_pid_and_clock(self):
        samples = {
            "[pid 20412] 09:15:30.003400 execve(\"/bin/true\", [\"true\"], 0x1 /* 2 vars */) = 0 <0.000014>": (20412, "09:15:30.003400"),
            "12345 10:42:01.100001 close(3) = 0": (12345, "10:42:01.100001"),
            "[pid  7] close(3)                       = 0": (7, None),
            "1698765432.123456 close(3) = 0": (None, "1698765432.123456"),
            "     0.000045 close(3) = 0": (None, "0.000045"),
            "10:42:01 close(3) = 0": (None, "10:42:01"),
            "close(3) = 0x7f0c2a3d5000": (None, None),
        }
        for text, expected in samples.items():
            trace = main.parse(text)
            self.assertEqual(len(trace.calls), 1, text)
            self.assertEqual((trace.calls[0].pid, trace.calls[0].clock), expected, text)
        self.assertEqual(main.parse("close(3) = 0x7f0c2a3d5000").calls[0].result, 0x7F0C2A3D5000)
        self.assertIsNone(main.parse("exit_group(1) = ?").calls[0].result)

    def test_split_args_respects_nesting_quotes_and_comments(self):
        text = '3<TCP:[412233]>, {sa_family=AF_INET, sin_port=htons(5432), sin_addr=inet_addr("10.0.0.5")}, 16'
        self.assertEqual(main.split_args(text), ["3<TCP:[412233]>", '{sa_family=AF_INET, sin_port=htons(5432), sin_addr=inet_addr("10.0.0.5")}', "16"])
        text = '"/bin/sh", ["sh", "-c", "a, b) c"], 0x7ffc /* 20 vars */'
        self.assertEqual(main.split_args(text), ['"/bin/sh"', '["sh", "-c", "a, b) c"]', "0x7ffc /* 20 vars */"])
        self.assertEqual(main.find_close('AT_FDCWD, "x)y", O_RDONLY) = 3'), 25)

    def test_unquote_decodes_strace_escapes(self):
        self.assertEqual(main.unquote('"  File \\"/app/app.py\\", line 9\\n"'), '  File "/app/app.py", line 9\n')
        self.assertEqual(main.unquote('"\\177ELF\\2\\1"...'), "\x7fELF\x02\x01")
        self.assertEqual(main.unquote('"\\x41\\t"'), "A\t")
        self.assertEqual(main.unquote('@"/tmp/abstract"'), "/tmp/abstract")
        self.assertIsNone(main.unquote("AT_FDCWD"))

    def test_targets_for_paths_fd_decorations_and_dirfd_joins(self):
        self.assertEqual(main.describe_target("stat", ['"/usr/lib/python3.12/os.py"', "0x7f"]), "/usr/lib/python3.12/os.py")
        self.assertEqual(main.describe_target("openat", ["AT_FDCWD", '"app.yml"', "O_RDONLY"]), "app.yml")
        self.assertEqual(main.describe_target("openat", ["5</app>", '"app.yml"', "O_RDONLY"]), "/app/app.yml")
        self.assertEqual(main.describe_target("read", ["3</etc/app.yml>", '"x"', "4096"]), "/etc/app.yml")
        self.assertEqual(main.describe_target("write", ["2</dev/pts/0>", '"oops"', "4"]), "/dev/pts/0")
        self.assertEqual(main.describe_target("execve", ['"/bin/migrate"', '["migrate"]', "0x1"]), "/bin/migrate")
        self.assertIsNone(main.describe_target("wait4", ["-1", "[{WIFEXITED(s)}]", "0", "NULL"]))
        self.assertIsNone(main.describe_target("close", ["3"]))

    def test_socket_addresses_inet_inet6_and_unix(self):
        inet = '{sa_family=AF_INET, sin_port=htons(5432), sin_addr=inet_addr("10.0.0.5")}'
        inet6 = '{sa_family=AF_INET6, sin6_port=htons(443), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "::1", &sin6_addr), sin6_scope_id=0}'
        unix = '{sa_family=AF_UNIX, sun_path="/var/run/docker.sock"}'
        self.assertEqual(main.parse_address(inet), "10.0.0.5:5432")
        self.assertEqual(main.parse_address(inet6), "[::1]:443")
        self.assertEqual(main.parse_address(unix), "/var/run/docker.sock")
        self.assertEqual(main.describe_target("connect", ["3<TCP:[1]>", inet, "16"]), "10.0.0.5:5432")
        self.assertEqual(main.describe_target("bind", ["4", '{sa_family=AF_INET, sin_port=htons(80), sin_addr=inet_addr("0.0.0.0")}', "16"]), "0.0.0.0:80")
        self.assertIsNone(main.parse_address("16"))

    def test_injected_marker_is_recorded(self):
        trace = main.parse('openat(AT_FDCWD, "/etc/app.yml", O_RDONLY) = -1 ENOENT (No such file or directory) (INJECTED)\n+++ exited with 1 +++')
        self.assertTrue(trace.calls[0].injected)
        self.assertIn("[injected by strace]", main.verdict(trace).text)


class EventTests(unittest.TestCase):
    def test_unfinished_and_resumed_halves_are_joined_across_prefix_styles(self):
        trace = load("fork-exec.txt")
        waits = [c for c in trace.calls if c.name == "wait4"]
        self.assertEqual(len(waits), 1)
        wait = waits[0]
        self.assertEqual((wait.pid, wait.result, wait.duration, wait.line), (20411, 20412, 0.0028, 7))
        self.assertEqual(wait.args[0], "-1")
        self.assertIn("WEXITSTATUS(s) == 127", wait.args[1])
        self.assertEqual(trace.unparsed, [])

    def test_exit_kill_signal_and_message_events(self):
        text = "\n".join([
            "[pid 11] +++ exited with 0 +++",
            "[pid 12] +++ killed by SIGSEGV (core dumped) +++",
            "[pid 13] +++ killed by SIGKILL +++",
            "--- SIGCHLD {si_signo=SIGCHLD, si_code=CLD_EXITED, si_pid=11, si_uid=0, si_status=0, si_utime=0, si_stime=0} ---",
            "--- stopped by SIGSTOP ---",
            "strace: Process 12 attached",
            "% time     seconds  usecs/call     calls    errors syscall",
        ])
        trace = main.parse(text)
        kinds = [(e.kind, e.detail, e.code) for e in trace.events]
        self.assertEqual(kinds[0], ("exit", "0", 0))
        self.assertEqual(kinds[1], ("killed", "SIGSEGV", None))
        self.assertEqual(kinds[2], ("killed", "SIGKILL", None))
        self.assertEqual(kinds[3], ("signal", "SIGCHLD", None))
        self.assertEqual(kinds[4], ("signal", "SIGSTOP", None))
        self.assertEqual(kinds[5], ("message", "Process 12 attached", None))
        self.assertEqual(kinds[6][0], "unparsed")
        self.assertEqual(main.signals(trace), {"SIGCHLD": 1, "SIGSTOP": 1})
        self.assertEqual([e.pid for e in trace.exits], [11, 12, 13])

    def test_leader_pid_is_inferred_for_unprefixed_lines(self):
        trace = load("fork-exec.txt")
        self.assertEqual({c.pid for c in trace.calls}, {20411, 20412})
        self.assertEqual([e.pid for e in trace.exits], [20412, 20411])
        self.assertEqual(trace.calls[0].pid, 20411)
        single = load("permission-denied.txt")
        self.assertTrue(all(c.pid is None for c in single.calls))

    def test_process_tree_from_clone_execve_and_exits(self):
        procs = main.processes(load("fork-exec.txt"))
        by_pid = {p.pid: p for p in procs}
        self.assertEqual(by_pid[20411].argv, ["sh", "-c", "migrate --up && echo done"])
        self.assertEqual((by_pid[20411].program, by_pid[20411].exit_code), ("/bin/sh", 127))
        self.assertEqual((by_pid[20412].parent, by_pid[20412].program, by_pid[20412].exit_code), (20411, "/bin/sh", 127))
        self.assertFalse(by_pid[20412].thread)
        threads = main.processes(main.parse("[pid 1] clone(child_stack=0x7f, flags=CLONE_VM|CLONE_FS|CLONE_THREAD|CLONE_SYSVSEM, parent_tid=[2]) = 2"))
        self.assertTrue(threads[1].thread)


class ReportTests(unittest.TestCase):
    def test_failures_grouped_by_errno_keep_search_order_and_hide_low_signal(self):
        trace = load("missing-config.txt")
        groups = main.failures_by_errno(trace)
        self.assertEqual([g.errno for g in groups], ["ENOENT"])
        self.assertEqual(groups[0].calls, 6)
        self.assertEqual([t.target for t in groups[0].targets][-3:], ["/etc/app/app.yml", "/home/deploy/.config/app/app.yml", "/app/app.yml"])
        self.assertEqual(groups[0].targets[0].syscall, "stat")
        everything = main.failures_by_errno(trace, include_low=True)
        self.assertEqual([(g.errno, g.low_signal) for g in everything], [("ENOENT", False), ("EINVAL", True)])
        self.assertEqual(everything[0].calls, 7)
        self.assertEqual(everything[1].targets[0].target, "/usr/bin/python3.12")

    def test_histogram_has_strace_c_columns(self):
        rows = main.histogram(load("refused-connection.txt"))
        by_name = {r.syscall: r for r in rows}
        self.assertEqual((by_name["connect"].calls, by_name["connect"].errors), (4, 3))
        self.assertEqual((by_name["socket"].calls, by_name["socket"].errors), (4, 0))
        self.assertEqual(rows[0].syscall, "clock_nanosleep")
        self.assertAlmostEqual(sum(r.percent for r in rows), 100.0, places=6)
        self.assertAlmostEqual(by_name["clock_nanosleep"].seconds, 2.002244)
        untimed = main.histogram(load("permission-denied.txt"))
        self.assertEqual((untimed[0].syscall, untimed[0].calls, untimed[0].errors), ("openat", 4, 1))
        self.assertIsNone(untimed[0].seconds)

    def test_slowest_marks_blocking_waits(self):
        trace = load("refused-connection.txt")
        slow = main.slowest(trace, 3)
        self.assertEqual([c.name for c in slow], ["clock_nanosleep", "clock_nanosleep", "execve"])
        self.assertIn("clock_nanosleep", main.BLOCKING_CALLS)
        text = main.render(trace)
        self.assertIn("waits for another process or I/O", text)
        self.assertIn("slowest calls (-T)", text)
        self.assertNotIn("slowest calls", main.render(load("permission-denied.txt")))

    def test_opened_files_per_pid_in_order_without_failures(self):
        opened = main.opened_files(load("missing-config.txt"))
        self.assertEqual(list(opened), [12345])
        self.assertEqual(opened[12345][0], "/etc/ld.so.cache")
        self.assertEqual(opened[12345][-1], "/app/app.py")
        self.assertNotIn("/app/app.yml", opened[12345])
        self.assertEqual(len(opened[12345]), 6)

    def test_stderr_writes_are_collected_from_fd_two_only(self):
        said = main.stderr_writes(load("missing-config.txt"))
        self.assertEqual(len(said), 4)
        self.assertEqual(said[1], (12345, '  File "/app/app.py", line 9, in <module>'))
        self.assertEqual(said[-1][1], "FileNotFoundError: [Errno 2] No such file or directory: '/app/app.yml'")
        self.assertEqual(main.stderr_writes(main.parse('write(1, "hello\\n", 6) = 6')), [])


class VerdictTests(unittest.TestCase):
    def test_missing_config_names_the_last_path_and_three_locations(self):
        result = main.verdict(load("missing-config.txt"))
        self.assertEqual((result.pid, result.syscall, result.errno, result.target, result.exit_code), (12345, "openat", "ENOENT", "/app/app.yml", 1))
        self.assertEqual(result.attempts, ["/etc/app/app.yml", "/home/deploy/.config/app/app.yml", "/app/app.yml"])
        self.assertTrue(result.text.startswith("python3 app.py (pid 12345) exited with code 1 because openat() of /app/app.yml returned ENOENT (No such file or directory) after trying 3 locations: /etc/app/app.yml, /home/deploy/.config/app/app.yml, /app/app.yml"), result.text)
        self.assertIn('it wrote to stderr: "FileNotFoundError: [Errno 2] No such file or directory: \'/app/app.yml\'"', result.text)
        self.assertIn("/proc/PID/cwd", result.hint)

    def test_refused_connection_names_address_and_attempts(self):
        result = main.verdict(load("refused-connection.txt"))
        self.assertEqual((result.syscall, result.errno, result.target, result.exit_code), ("connect", "ECONNREFUSED", "10.0.0.5:5432", 2))
        self.assertIn("python3 worker.py (the traced process) exited with code 2 because connect() to 10.0.0.5:5432 returned ECONNREFUSED (Connection refused) after 3 attempts", result.text)
        self.assertIn('fatal: database unreachable, giving up', result.text)
        self.assertIn("nothing listens", result.hint)

    def test_permission_denied_verdict(self):
        result = main.verdict(load("permission-denied.txt"))
        self.assertEqual((result.syscall, result.errno, result.target, result.exit_code, result.attempts), ("openat", "EACCES", "/var/log/app/app.log", 3, ["/var/log/app/app.log"]))
        self.assertTrue(result.text.startswith("app --log (the traced process) exited with code 3 because openat() of /var/log/app/app.log returned EACCES (Permission denied); it wrote to stderr"), result.text)
        self.assertIn("ls -ld", result.hint)

    def test_fork_exec_blames_the_child_and_its_path_search(self):
        result = main.verdict(load("fork-exec.txt"))
        self.assertEqual((result.pid, result.syscall, result.errno, result.target, result.exit_code), (20412, "execve", "ENOENT", "/bin/migrate", 127))
        self.assertEqual(len(result.attempts), 6)
        self.assertTrue(result.text.startswith("sh -c (pid 20412, child of pid 20411) exited with code 127 because execve() of /bin/migrate returned ENOENT (No such file or directory) after trying 6 locations: /usr/local/sbin/migrate"), result.text)
        self.assertIn('"sh: 1: migrate: not found"', result.text)

    def test_clean_trace_and_recovered_failure_and_killed_process(self):
        clean = main.parse('openat(AT_FDCWD, "/etc/hosts", O_RDONLY) = 3\nread(3, "x", 1) = -1 EAGAIN (Resource temporarily unavailable)\n+++ exited with 0 +++')
        self.assertEqual(main.verdict(clean).text, "no failing system calls worth reporting: 2 calls, 1 low-signal errors hidden; the traced process exit 0")
        recovered = main.parse('openat(AT_FDCWD, "/etc/a.yml", O_RDONLY) = -1 ENOENT (No such file or directory)\nopenat(AT_FDCWD, "/etc/b.yml", O_RDONLY) = 3\n+++ exited with 0 +++')
        self.assertIn("exited with code 0; the last failing call, openat() of /etc/a.yml, returned ENOENT (No such file or directory), and the program carried on", main.verdict(recovered).text)
        killed = main.parse('[pid 9] connect(3, {sa_family=AF_INET, sin_port=htons(80), sin_addr=inet_addr("10.1.1.1")}, 16) = -1 ETIMEDOUT (Connection timed out)\n[pid 9] +++ killed by SIGKILL +++')
        text = main.verdict(killed).text
        self.assertTrue(text.startswith("pid 9 was killed by SIGKILL; the last failing call was connect() to 10.1.1.1:80, which returned ETIMEDOUT"), text)
        running = main.parse('openat(AT_FDCWD, "/x", O_RDONLY) = -1 ENOENT (No such file or directory)')
        self.assertIn("has not exited in this trace", main.verdict(running).text)

    def test_attach_denied_message_gets_the_capability_hint(self):
        trace = main.parse("strace: attach: ptrace(PTRACE_SEIZE, 1): Operation not permitted\n")
        result = main.verdict(trace)
        self.assertIn("CAP_SYS_PTRACE", result.text)
        self.assertIn("--cap-add SYS_PTRACE", result.text)
        self.assertEqual(result.errno, "EPERM")
        self.assertIn("strace: attach: ptrace(PTRACE_SEIZE, 1): Operation not permitted", main.render(trace))


class CliTests(unittest.TestCase):
    def run_main(self, argv, **kwargs):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(argv, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def test_text_report_has_every_section(self):
        code, out, _ = self.run_main([os.path.join(FIXTURES, "fork-exec.txt")])
        self.assertEqual(code, 0)
        for heading in ("tracelens: ", "verdict: ", "hint (ENOENT)", "processes", "failures by errno", "slowest calls (-T)", "syscall histogram (like strace -c)", "files opened per process", "signals: SIGCHLD x1", "strace: Process 20412 attached"):
            self.assertIn(heading, out, heading)
        self.assertIn("pid 20412", out)
        self.assertIn("exit 127", out)
        self.assertIn("1 low-signal hidden; --all shows them", out)
        code, verdict_only, _ = self.run_main([os.path.join(FIXTURES, "fork-exec.txt"), "--verdict"])
        self.assertEqual(code, 0)
        self.assertEqual(len(verdict_only.strip().splitlines()), 1)

    def test_json_output_shape(self):
        code, out, _ = self.run_main([os.path.join(FIXTURES, "missing-config.txt"), "--json", "--top", "3"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual((report["calls"], report["unparsed"], report["lines"]), (36, 0, 37))
        self.assertEqual(report["verdict"]["errno"], "ENOENT")
        self.assertEqual(report["exits"], [{"pid": 12345, "code": 1, "signal": None, "line": 37}])
        self.assertEqual(report["failures"][0]["targets"][-1]["target"], "/app/app.yml")
        self.assertEqual(len(report["slowest"]), 3)
        self.assertEqual(report["files"]["12345"][-1], "/app/app.py")
        self.assertEqual(report["processes"][0]["argv"], ["python3", "app.py"])
        self.assertEqual(report["stderr"][-1]["pid"], 12345)
        self.assertEqual(sum(r["calls"] for r in report["histogram"]), 36)

    def test_stdin_missing_file_and_empty_input_exit_codes(self):
        code, _, err = self.run_main(["/nonexistent/trace.txt"])
        self.assertEqual(code, 1)
        self.assertIn("cannot read /nonexistent/trace.txt", err)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write("not a trace\n")
        self.addCleanup(os.unlink, handle.name)
        code, _, err = self.run_main([handle.name])
        self.assertEqual(code, 0)
        empty = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        empty.close()
        self.addCleanup(os.unlink, empty.name)
        code, _, err = self.run_main([empty.name])
        self.assertEqual(code, 1)
        self.assertIn("no strace lines found", err)

    def test_run_mode_uses_the_injected_runner_and_reports_when_strace_is_missing(self):
        seen = {}

        def fake_run(command, trace_set, output):
            seen["command"], seen["trace_set"], seen["output"] = list(command), trace_set, output
            return fixture("permission-denied.txt"), 3

        code, out, _ = self.run_main(["--verdict", "--run", "--", "app", "--log", "/var/log/app/app.log"], run=fake_run)
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"command": ["app", "--log", "/var/log/app/app.log"], "trace_set": main.DEFAULT_TRACE_SET, "output": None})
        self.assertIn("EACCES", out)

        def no_strace(command, trace_set, output):
            raise RuntimeError("strace not found; Debian/Ubuntu: sudo apt-get install -y strace")

        code, _, err = self.run_main(["--run", "true"], run=no_strace)
        self.assertEqual(code, 2)
        self.assertIn("strace not found", err)

    def test_run_strace_builds_the_command_and_reads_the_output_file(self):
        calls = []

        class Done:
            returncode = 1

        def runner(command):
            calls.append(command)
            with open(command[command.index("-o") + 1], "w", encoding="utf-8") as handle:
                handle.write(fixture("refused-connection.txt"))
            return Done()

        text, code = main.run_strace(["python3", "worker.py"], "%network,write", runner=runner, which=lambda name: "/usr/bin/strace")
        self.assertEqual(code, 1)
        self.assertIn("ECONNREFUSED", text)
        self.assertEqual(calls[0][:7], ["strace", "-f", "-tt", "-T", "-y", "-s", "200"])
        self.assertEqual(calls[0][-4:], ["-e", "trace=%network,write", "python3", "worker.py"])
        self.assertFalse(os.path.exists(calls[0][calls[0].index("-o") + 1]))
        with self.assertRaises(RuntimeError):
            main.run_strace(["true"], runner=runner, which=lambda name: None)


if __name__ == "__main__":
    unittest.main()
