import json
import os
import shutil
import signal
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

TERM = int(signal.SIGTERM)
KILL = int(signal.SIGKILL)


def unit(name, **fields):
    u = main.Unit(name=name, path=f"/units/{name}")
    for key, value in fields.items():
        setattr(u, key, value)
    return u


def ex(*argv, ignore=False):
    return main.Exec(list(argv), ignore)


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeOS:
    def __init__(self, missing=()):
        self.next_pid = 1000
        self.spawned = []
        self.killed = []
        self.missing = set(missing)

    def spawn(self, unit_name, argv, env, cwd):
        if argv[0] in self.missing:
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        self.next_pid += 1
        self.spawned.append((self.next_pid, unit_name, list(argv), env, cwd))
        return self.next_pid

    def kill(self, pid, signum):
        self.killed.append((pid, signum))


def make(units, read_file=None, missing=()):
    clock = FakeClock()
    fake = FakeOS(missing)
    journal = main.Journal()
    manager = main.Manager({u.name: u for u in units}, fake.spawn, fake.kill, clock, journal, read_file or (lambda path: ""))
    return manager, fake, clock


def lines(manager, who="miniunit"):
    return [e.line for e in manager.journal.entries if e.unit == who]


class ParserTests(unittest.TestCase):
    def test_sections_keys_comments_and_continuations(self):
        text = """# a comment
; another
[Unit]
Description=Demo web API
After=network.target \\
      db.service

[Service]
Type = simple
ExecStart=/usr/bin/python3 -m http.server 8080
WorkingDirectory=/srv/web
TimeoutStopSec=5

[Install]
WantedBy=multi-user.target
"""
        u = main.parse_unit_text(text, "web.service", "/etc/systemd/system/web.service")
        self.assertEqual((u.name, u.path, u.description), ("web.service", "/etc/systemd/system/web.service", "Demo web API"))
        self.assertEqual(u.after, ["network.target", "db.service"])
        self.assertEqual(u.exec_start, [ex("/usr/bin/python3", "-m", "http.server", "8080")])
        self.assertEqual((u.service_type, u.working_directory, u.timeout_stop_sec, u.restart, u.restart_sec), ("simple", "/srv/web", 5.0, "no", 0.1))
        self.assertEqual(u.warnings, [])

    def test_repeated_keys_append_and_an_empty_assignment_resets(self):
        text = """[Unit]
Requires=db.service
Requires=cache.service
Wants=a.service
Wants=
Wants=b.service
[Service]
Type=oneshot
ExecStartPre=/bin/echo one
ExecStartPre=-/bin/echo two
ExecStart=/bin/echo three
ExecStart=/bin/echo four
Description=wrong section
"""
        u = main.parse_unit_text(text, "job.service")
        self.assertEqual(u.requires, ["db.service", "cache.service"])
        self.assertEqual(u.wants, ["b.service"])
        self.assertEqual(u.exec_start_pre, [ex("/bin/echo", "one"), ex("/bin/echo", "two", ignore=True)])
        self.assertEqual([e.argv[-1] for e in u.exec_start], ["three", "four"])
        self.assertEqual(u.warnings, ["line 13: unknown key Description= in [Service] ignored"])

    def test_environment_quotes_environment_file_and_the_minimal_environment(self):
        text = """[Service]
ExecStart=/srv/app
Environment="GREETING=hello world" PORT=8080
Environment=DEBUG=1
EnvironmentFile=/etc/app.env
EnvironmentFile=-/etc/optional.env
"""
        u = main.parse_unit_text(text, "app.service")
        self.assertEqual(u.environment, {"GREETING": "hello world", "PORT": "8080", "DEBUG": "1"})
        self.assertEqual(u.environment_files, [("/etc/app.env", False), ("/etc/optional.env", True)])
        files = {"/etc/app.env": "# comment\nexport DB_URL=postgres://db/app\nPORT=9000\n"}

        def read_file(path):
            if path not in files:
                raise FileNotFoundError(2, "No such file or directory", path)
            return files[path]

        env = main.unit_environment(u, read_file)
        self.assertEqual(env["DB_URL"], "postgres://db/app")
        self.assertEqual(env["PORT"], "8080", "Environment= wins over EnvironmentFile=")
        self.assertEqual(env["PATH"], main.DEFAULT_PATH)
        self.assertNotIn("HOME", env)
        u.environment_files = [("/etc/required.env", False)]
        with self.assertRaises(main.UnitError):
            main.unit_environment(u, read_file)

    def test_time_spans(self):
        self.assertEqual(main.parse_time_span("5"), 5.0)
        self.assertEqual(main.parse_time_span("500ms"), 0.5)
        self.assertEqual(main.parse_time_span("1min 30s"), 90.0)
        self.assertEqual(main.parse_time_span("2h"), 7200.0)
        self.assertIsNone(main.parse_time_span("infinity"))
        for bad in ("abc", "5x", ""):
            with self.assertRaises(main.UnitError):
                main.parse_time_span(bad)

    def test_validation_errors_name_the_unit_and_the_rule(self):
        cases = {
            "[Service]\nType=simple\n": "needs ExecStart=",
            "[Service]\nExecStart=/a\nExecStart=/b\n": "Type=simple allows exactly one ExecStart=",
            "[Service]\nExecStart=/a\nRestart=sometimes\n": "Restart=sometimes is not supported",
            "[Service]\nType=oneshot\nExecStart=/a\nRestart=always\n": "Restart=always is not allowed with Type=oneshot",
            "[Service]\nType=forking\nExecStart=/a\n": "Type=forking is not supported",
            "[Service]\nExecStart=@/a b\n": "Exec prefix '@' is not supported",
            "[Service]\nExecStart=/a\nRestartSec=soon\n": "invalid time span",
            "ExecStart=/a\n": "assignment outside of a section",
            "[Service]\nExecStart=/a\njunk\n": "expected Key=Value",
        }
        for text, message in cases.items():
            with self.assertRaises(main.UnitError) as caught:
                main.parse_unit_text(text, "x.service")
            self.assertIn(message, str(caught.exception), text)


class GraphTests(unittest.TestCase):
    def units(self):
        return {
            "db.service": unit("db.service", exec_start=[ex("/db")]),
            "migrate.service": unit("migrate.service", exec_start=[ex("/migrate")], after=["db.service"]),
            "web.service": unit("web.service", exec_start=[ex("/web")], requires=["db.service"], wants=["migrate.service", "metrics.service"], after=["migrate.service", "network.target"]),
            "loose.service": unit("loose.service", exec_start=[ex("/loose")], requires=["db.service"]),
        }

    def test_transaction_pulls_in_requires_and_wants_and_start_order_follows_after_only(self):
        units = self.units()
        selected = main.transaction(units, ["web.service"])
        self.assertEqual(sorted(selected), ["db.service", "migrate.service", "web.service"])
        self.assertEqual(main.start_order(units, selected), ["db.service", "migrate.service", "web.service"])
        self.assertEqual(main.start_order(units, ["loose.service", "db.service"]), ["db.service", "loose.service"], "Requires= without After= gives no order beyond alphabetical")
        with self.assertRaises(main.UnitError) as caught:
            main.transaction(units, ["nothere.service"])
        self.assertIn("Unit nothere.service not found", str(caught.exception))

    def test_ordering_cycle_is_reported_with_its_path(self):
        units = {
            "a.service": unit("a.service", exec_start=[ex("/a")], after=["c.service"]),
            "b.service": unit("b.service", exec_start=[ex("/b")], after=["a.service"]),
            "c.service": unit("c.service", exec_start=[ex("/c")], after=["b.service"]),
            "d.service": unit("d.service", exec_start=[ex("/d")]),
        }
        with self.assertRaises(main.UnitError) as caught:
            main.start_order(units, list(units))
        self.assertIn("ordering cycle: a.service -> c.service -> b.service -> a.service", str(caught.exception))

    def test_verify_reports_missing_requires_as_error_and_the_rest_as_warnings(self):
        units = self.units()
        self.assertIn("start order", main.render_graph(units, ["loose.service"]))
        graph = main.render_graph(units, ["web.service"])
        self.assertIn("├─db.service               Requires\n", graph)
        self.assertIn("├─metrics.service          Wants  (not a local unit: ignored)", graph)
        self.assertIn("└─network.target           After  (not a local unit: ordering only)", graph)
        units["web.service"].requires.append("ghost.service")
        errors, warnings = main.verify(units)
        self.assertEqual(errors, ["web.service: Requires=ghost.service but ghost.service does not exist"])
        self.assertIn("web.service: Wants=metrics.service but metrics.service does not exist; it will be ignored", warnings)
        self.assertTrue(any(w.startswith("web.service: Requires=db.service without After=db.service") for w in warnings))
        self.assertTrue(any(w.startswith("loose.service: Requires=db.service without After=db.service") for w in warnings))
        self.assertIn("db.service: executable /db not found", warnings)


class ActivationTests(unittest.TestCase):
    def test_simple_unit_runs_start_pre_then_counts_as_active_when_forked(self):
        web = unit("web.service", description="Web API", exec_start_pre=[ex("/bin/true")], exec_start=[ex("/srv", "web")], environment={"PORT": "8080"}, working_directory="/srv")
        m, fake, _ = make([web])
        self.assertEqual(m.start(["web.service"]), ["web.service"])
        m.tick()
        state = m.states["web.service"]
        self.assertEqual((state.active, state.sub, state.control_pid, state.main_pid), ("activating", "start-pre", 1001, None))
        self.assertEqual(fake.spawned[0][1:3], ("web.service", ["/bin/true"]))
        m.handle_exit(1001, 0)
        self.assertEqual((state.active, state.sub, state.main_pid), ("active", "running", 1002))
        pid, _, argv, env, cwd = fake.spawned[1]
        self.assertEqual((argv, env["PORT"], env["PATH"], cwd), (["/srv", "web"], "8080", main.DEFAULT_PATH, "/srv"))
        self.assertEqual([(e.step, e.code) for e in state.execs], [("start-pre", 0)])
        self.assertIn("Started web.service", lines(m))
        self.assertFalse(m.busy() is False)

    def test_failed_start_pre_fails_the_unit_unless_prefixed_with_minus(self):
        strict = unit("strict.service", exec_start_pre=[ex("/check")], exec_start=[ex("/srv")])
        lenient = unit("lenient.service", exec_start_pre=[ex("/check", ignore=True)], exec_start=[ex("/srv")])
        m, fake, _ = make([strict, lenient])
        m.start(["strict.service", "lenient.service"])
        m.tick()
        m.handle_exit(1001, 1)
        m.handle_exit(1002, 1)
        self.assertEqual((m.states["strict.service"].active, m.states["strict.service"].result), ("failed", "exit-code"))
        self.assertEqual((m.states["lenient.service"].active, m.states["lenient.service"].main_pid), ("active", 1003))
        self.assertIn("Failed to start strict.service: exit-code", lines(m))

    def test_oneshot_holds_dependents_until_it_exits(self):
        migrate = unit("migrate.service", service_type="oneshot", exec_start=[ex("/migrate")])
        web = unit("web.service", exec_start=[ex("/web")], wants=["migrate.service"], after=["migrate.service"])
        m, fake, _ = make([migrate, web])
        self.assertEqual(m.start(["web.service"]), ["migrate.service", "web.service"])
        m.tick()
        self.assertEqual((m.states["migrate.service"].active, m.states["migrate.service"].sub, m.states["migrate.service"].main_pid), ("activating", "start", 1001))
        self.assertEqual((m.states["web.service"].active, m.states["web.service"].job), ("inactive", "start"))
        self.assertEqual(len(fake.spawned), 1, "web must wait for the oneshot to finish")
        m.handle_exit(1001, 0)
        self.assertEqual((m.states["migrate.service"].active, m.states["migrate.service"].sub, m.states["migrate.service"].result), ("inactive", "dead", "success"))
        m.tick()
        self.assertEqual((m.states["web.service"].active, m.states["web.service"].main_pid), ("active", 1002))
        remain = unit("setup.service", service_type="oneshot", remain_after_exit=True, exec_start=[ex("/a"), ex("/b")])
        m2, fake2, _ = make([remain])
        m2.start(["setup.service"])
        m2.tick()
        m2.handle_exit(1001, 0)
        self.assertEqual(fake2.spawned[1][2], ["/b"], "oneshot runs its ExecStart lines in sequence")
        m2.handle_exit(1002, 0)
        self.assertEqual((m2.states["setup.service"].active, m2.states["setup.service"].sub), ("active", "exited"))

    def test_requires_plus_after_cancels_on_dependency_failure_but_wants_and_bare_requires_do_not(self):
        db = unit("db.service", service_type="oneshot", exec_start=[ex("/db")])
        web = unit("web.service", exec_start=[ex("/web")], requires=["db.service"], after=["db.service"])
        cache = unit("cache.service", exec_start=[ex("/cache")], wants=["db.service"], after=["db.service"])
        loose = unit("loose.service", exec_start=[ex("/loose")], requires=["db.service"])
        m, fake, _ = make([db, web, cache, loose])
        m.start(["web.service", "cache.service", "loose.service"])
        m.tick()
        self.assertEqual([s[1] for s in fake.spawned], ["db.service", "loose.service"], "no After= means no waiting")
        m.handle_exit(1001, 1)
        self.assertEqual((m.states["db.service"].active, m.states["db.service"].result), ("failed", "exit-code"))
        m.tick()
        web_state, cache_state = m.states["web.service"], m.states["cache.service"]
        self.assertEqual((web_state.active, web_state.sub, web_state.result, web_state.job), ("inactive", "dead", "dependency", None))
        self.assertEqual((cache_state.active, cache_state.main_pid), ("active", 1003))
        self.assertIn("Dependency failed for web.service: db.service failed to start", lines(m))

    def test_exec_failure_is_reported_as_status_203(self):
        app = unit("app.service", exec_start=[ex("/missing")])
        m, fake, _ = make([app], missing=["/missing"])
        m.start(["app.service"])
        m.tick()
        state = m.states["app.service"]
        self.assertEqual((state.active, state.result), ("failed", "exit-code"))
        self.assertEqual([(e.step, e.code) for e in state.execs], [("start", main.EXIT_EXEC)])
        self.assertIn("status=203/EXEC", main.render_status(m.status_dict("app.service"), [], 100.0))
        self.assertTrue(any("Failed to execute /missing" in e.line for e in m.journal.for_unit("app.service")))


class RestartTests(unittest.TestCase):
    def test_on_failure_restarts_after_restart_sec_and_counts(self):
        flaky = unit("flaky.service", exec_start=[ex("/flaky")], restart="on-failure", restart_sec=2.0)
        m, fake, clock = make([flaky])
        m.start(["flaky.service"])
        m.tick()
        m.handle_exit(1001, 1)
        state = m.states["flaky.service"]
        self.assertEqual((state.active, state.sub, state.restarts, state.restart_at), ("activating", "auto-restart", 1, 102.0))
        self.assertEqual(m.next_deadline(), 102.0)
        clock.t += 1
        m.tick()
        self.assertEqual(len(fake.spawned), 1, "not before RestartSec")
        clock.t += 1
        m.tick()
        self.assertEqual((state.active, state.main_pid, len(fake.spawned)), ("active", 1002, 2))
        m.handle_exit(1002, 0)
        self.assertEqual((state.active, state.sub, state.result, state.restarts), ("inactive", "dead", "success", 1), "a clean exit is not a failure")
        self.assertIn("flaky.service: Scheduled restart job, restart counter is at 1.", lines(m))

    def test_sigterm_sighup_sigint_sigpipe_count_as_clean_for_on_failure(self):
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGPIPE):
            self.assertTrue(main.is_clean_exit(-int(signum)))
        self.assertFalse(main.is_clean_exit(-int(signal.SIGKILL)))
        self.assertFalse(main.is_clean_exit(-int(signal.SIGSEGV)))
        self.assertFalse(main.is_clean_exit(1))
        svc = unit("svc.service", exec_start=[ex("/svc")], restart="on-failure")
        m, _, _ = make([svc])
        m.start(["svc.service"])
        m.tick()
        m.handle_exit(1001, -TERM)
        self.assertEqual((m.states["svc.service"].active, m.states["svc.service"].result), ("inactive", "success"))
        m.start(["svc.service"])
        m.tick()
        m.handle_exit(1002, -KILL)
        self.assertEqual((m.states["svc.service"].sub, m.states["svc.service"].result), ("auto-restart", "signal"))
        always = unit("always.service", exec_start=[ex("/a")], restart="always")
        m2, _, _ = make([always])
        m2.start(["always.service"])
        m2.tick()
        m2.handle_exit(1001, 0)
        self.assertEqual(m2.states["always.service"].sub, "auto-restart", "Restart=always restarts even after exit 0")

    def test_start_limit_burst_turns_a_restart_loop_into_failed(self):
        flaky = unit("flaky.service", service_type="oneshot", exec_start=[ex("/flaky")], restart="on-failure", restart_sec=1.0, start_limit_burst=3, start_limit_interval=10.0)
        m, fake, clock = make([flaky])
        m.start(["flaky.service"])
        state = m.states["flaky.service"]
        for attempt in range(3):
            m.tick()
            self.assertEqual(len(fake.spawned), attempt + 1)
            m.handle_exit(1001 + attempt, 1)
            self.assertEqual(state.sub, "auto-restart")
            clock.t += 1
        m.tick()
        self.assertEqual((state.active, state.sub, state.result, state.restarts, len(fake.spawned)), ("failed", "failed", "start-limit-hit", 3, 3))
        self.assertIn("flaky.service: Start request repeated too quickly (3 starts within 10.0s)", lines(m))
        clock.t += 20
        m.start(["flaky.service"])
        m.tick()
        self.assertEqual((state.active, len(fake.spawned)), ("activating", 4), "outside the interval the unit may start again")


class StopTests(unittest.TestCase):
    def test_stop_sends_sigterm_escalates_after_timeout_and_runs_stop_post(self):
        web = unit("web.service", exec_start=[ex("/srv")], exec_stop_post=[ex("/cleanup")], timeout_stop_sec=5.0)
        m, fake, clock = make([web])
        m.start(["web.service"])
        m.tick()
        self.assertEqual(m.stop(["web.service"]), ["web.service"])
        m.tick()
        state = m.states["web.service"]
        self.assertEqual(fake.killed, [(1001, TERM)])
        self.assertEqual((state.active, state.sub, state.stop_deadline), ("deactivating", "stop-sigterm", 105.0))
        clock.t += 4
        m.tick()
        self.assertEqual(len(fake.killed), 1)
        clock.t += 2
        m.tick()
        self.assertEqual(fake.killed[-1], (1001, KILL))
        self.assertEqual((state.sub, state.result), ("stop-sigkill", "timeout"))
        m.handle_exit(1001, -KILL)
        self.assertEqual((state.active, state.sub, state.control_pid), ("deactivating", "stop-post", 1002))
        env = fake.spawned[-1][3]
        self.assertEqual((fake.spawned[-1][2], env["SERVICE_RESULT"], env["EXIT_CODE"], env["EXIT_STATUS"]), (["/cleanup"], "timeout", "killed", "KILL"))
        m.handle_exit(1002, 0)
        self.assertEqual((state.active, state.sub, state.result), ("failed", "failed", "timeout"))
        self.assertIn("Stopped web.service (Result: timeout)", lines(m))
        self.assertFalse(m.busy())

    def test_prompt_exit_on_sigterm_is_a_clean_stop_and_suspends_restart_always(self):
        web = unit("web.service", exec_start=[ex("/srv")], exec_stop_post=[ex("/cleanup")], restart="always")
        m, fake, _ = make([web])
        m.start(["web.service"])
        m.tick()
        m.stop(["web.service"])
        m.tick()
        m.handle_exit(1001, -TERM)
        env = fake.spawned[-1][3]
        self.assertEqual((env["SERVICE_RESULT"], env["EXIT_CODE"], env["EXIT_STATUS"]), ("success", "killed", "TERM"))
        m.handle_exit(1002, 0)
        state = m.states["web.service"]
        self.assertEqual((state.active, state.sub, state.result, state.restarts, state.restart_at), ("inactive", "dead", "success", 0, None))
        queued = unit("queued.service", exec_start=[ex("/q")], after=["web.service"])
        m2, _, _ = make([web, queued])
        m2.start(["queued.service", "web.service"])
        m2.stop(["queued.service"])
        self.assertEqual((m2.states["queued.service"].job, m2.states["queued.service"].active), (None, "inactive"))
        self.assertIn("Cancelled start job for queued.service", lines(m2))

    def test_stopping_a_required_unit_stops_its_dependents_first(self):
        db = unit("db.service", exec_start=[ex("/db")])
        web = unit("web.service", exec_start=[ex("/web")], requires=["db.service"], after=["db.service"])
        m, fake, _ = make([db, web])
        m.start(["web.service"])
        m.tick()
        self.assertEqual(sorted(s.main_pid for s in m.states.values()), [1001, 1002])
        self.assertEqual(m.stop(["db.service"]), ["db.service", "web.service"])
        m.tick()
        self.assertEqual(fake.killed, [(1002, TERM)], "web goes first; db waits")
        self.assertEqual(m.states["db.service"].job, "stop")
        m.handle_exit(1002, -TERM)
        m.tick()
        self.assertEqual(fake.killed, [(1002, TERM), (1001, TERM)])
        m.handle_exit(1001, -TERM)
        self.assertEqual([s.active for s in m.states.values()], ["inactive", "inactive"])
        self.assertEqual([l for l in lines(m) if l.startswith("Stopping")], ["Stopping web.service", "Stopping db.service"])
        m.apply_control(['{"action": "start", "units": ["web.service"]}'])
        m.tick()
        self.assertEqual([s[1] for s in fake.spawned[2:]], ["db.service", "web.service"])
        m.apply_control(['{"action": "stop"}', ""])
        self.assertEqual([s.job for s in m.states.values()], ["stop", "stop"])


class RenderingTests(unittest.TestCase):
    def test_journal_entries_render_like_journalctl(self):
        journal = main.Journal()
        journal.record("web.service", 1002, "stdout", "listening on 8080", 1700000000.0)
        journal.record("db.service", 1001, "stderr", "ready", 1700000001.0)
        journal.record("web.service", 1002, "stdout", "request 1", 1700000002.0)
        self.assertTrue(main.format_journal_line(journal.entries[0]).endswith(" web.service[1002]: listening on 8080"))
        self.assertEqual([e.line for e in journal.for_unit("web.service")], ["listening on 8080", "request 1"])
        self.assertEqual([e.line for e in journal.for_unit(None, last=1)], ["request 1"])

    def test_status_renders_like_systemctl_and_as_json(self):
        web = unit("web.service", description="Web API", exec_start_pre=[ex("/bin/true")], exec_start=[ex("/srv")], restart="on-failure")
        m, fake, clock = make([web])
        m.start(["web.service"])
        m.tick()
        m.handle_exit(1001, 0)
        m.journal.record("web.service", 1002, "stdout", "listening on 8080", clock())
        clock.t += 75
        text = main.render_status(m.status_dict("web.service"), m.journal.for_unit("web.service", 5), clock())
        self.assertTrue(text.startswith("● web.service - Web API\n"))
        self.assertIn("Loaded: loaded (/units/web.service; Restart=on-failure; Type=simple)", text)
        self.assertIn("Active: active (running) since ", text)
        self.assertIn("; 1min 15s ago", text)
        self.assertIn("Process: 1001 ExecStartPre=/bin/true (code=exited, status=0/SUCCESS)", text)
        self.assertIn("Main PID: 1002", text)
        self.assertIn("Restarts: 0", text)
        self.assertTrue(text.endswith("web.service[1002]: listening on 8080"))
        m.handle_exit(1002, -int(signal.SIGSEGV))
        info = m.status_dict("web.service")
        self.assertEqual((info["active"], info["sub"], info["result"], info["restarts"], info["main_pid"]), ("activating", "auto-restart", "signal", 1, None))
        self.assertEqual(info["execs"][-1], {"step": "start", "argv": ["/srv"], "pid": 1002, "code": -int(signal.SIGSEGV)})
        self.assertEqual(main.describe_exit(-TERM), "code=killed, signal=15/TERM")
        self.assertEqual(main.describe_exit(3), "code=exited, status=3/FAILURE")
        failed = dict(info, active="failed", sub="failed", result="exit-code", since=None)
        self.assertIn("× web.service", main.render_status(failed, [], clock()))
        self.assertIn("Active: failed (failed) (Result: exit-code)", main.render_status(failed, [], clock()))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="miniunit-")
        self.units = os.path.join(self.root, "units")
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.units)
        self.addCleanup(shutil.rmtree, self.root)

    def write(self, name, text):
        with open(os.path.join(self.units, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def run_cli(self, *argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_verify_graph_and_error_exit_codes(self):
        self.write("db.service", "[Unit]\nDescription=DB\n[Service]\nExecStart=/bin/sh -c 'exec sleep 30'\n")
        self.write("web.service", "[Unit]\nRequires=db.service\nAfter=db.service network.target\n[Service]\nExecStart=/bin/sh -c 'exec sleep 30'\n")
        code, out, _ = self.run_cli("--units", self.units, "verify")
        self.assertEqual((code, out.strip().splitlines()[-1]), (0, "2 unit(s), 0 error(s), 0 warning(s)"))
        code, out, _ = self.run_cli("graph", "--units", self.units, "--json", "web.service")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["order"], ["db.service", "web.service"])
        code, out, _ = self.run_cli("--units", self.units, "graph")
        self.assertIn("start order: db.service -> web.service", out)
        self.write("web.service", "[Unit]\nRequires=ghost.service\nAfter=db.service\n[Service]\nExecStart=/bin/sh\n")
        code, out, _ = self.run_cli("--units", self.units, "--json", "verify")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"], ["web.service: Requires=ghost.service but ghost.service does not exist"])
        self.write("web.service", "[Service]\nType=oneshot\nExecStart=/bin/sh\nRestart=always\n")
        code, _, err = self.run_cli("--units", self.units, "verify")
        self.assertEqual(code, 2)
        self.assertIn("Restart=always is not allowed with Type=oneshot", err)
        code, _, err = self.run_cli("--units", os.path.join(self.root, "nope"), "graph")
        self.assertEqual((code, "not found" in err), (2, True))
        code, _, err = self.run_cli("status", "--state", self.state)
        self.assertEqual((code, "no state in" in err), (2, True))
        code, _, err = self.run_cli("stop", "--state", self.state)
        self.assertEqual((code, "no running manager" in err), (2, True))

    def test_start_supervises_real_processes_then_status_and_journal_read_the_state_dir(self):
        self.write("hello.service", "[Unit]\nDescription=Says hello\n[Service]\nEnvironment=NAME=world\nExecStart=/bin/sh -c 'echo \"hello $NAME\"; exec sleep 30'\nExecStopPost=/bin/sh -c 'echo \"post $SERVICE_RESULT $EXIT_CODE $EXIT_STATUS\"'\nTimeoutStopSec=5\n")
        self.write("once.service", "[Service]\nType=oneshot\nExecStart=/bin/sh -c 'echo once; exit 0'\n")
        code, _, err = self.run_cli("start", "--units", self.units, "--state", self.state, "--for", "0.7", "hello.service", "once.service")
        self.assertEqual(code, 0, err)
        code, out, _ = self.run_cli("status", "--state", self.state, "--json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual((report["hello.service"]["active"], report["hello.service"]["sub"], report["hello.service"]["result"]), ("inactive", "dead", "success"))
        self.assertEqual((report["once.service"]["active"], report["once.service"]["result"]), ("inactive", "success"))
        self.assertEqual(report["hello.service"]["execs"][0]["code"], -TERM)
        code, out, _ = self.run_cli("journal", "--state", self.state, "-u", "hello.service")
        self.assertEqual(code, 0)
        self.assertIn("hello.service[", out)
        self.assertIn("]: hello world", out)
        self.assertIn("]: post success killed TERM", out)
        code, out, _ = self.run_cli("journal", "--state", self.state, "--json", "-n", "1")
        self.assertEqual(json.loads(out.strip())["unit"], "miniunit")
        code, out, _ = self.run_cli("status", "--state", self.state, "hello.service")
        self.assertEqual(code, 0)
        self.assertIn("manager: not running (last run shown)", out)
        self.assertIn("○ hello.service - Says hello", out)
        self.assertIn("Active: inactive (dead) since", out)


if __name__ == "__main__":
    unittest.main()
