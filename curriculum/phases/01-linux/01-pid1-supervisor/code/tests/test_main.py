import os
import signal
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


def exited(code):
    return code << 8


def signaled(signum):
    return signum


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeOS:
    def __init__(self):
        self.next_pid = 1000
        self.spawned = []
        self.killed = []

    def spawn(self, command):
        self.next_pid += 1
        self.spawned.append((self.next_pid, list(command)))
        return self.next_pid

    def kill(self, pid, signum):
        self.killed.append((pid, signum))


def make(services, grace=10.0):
    clock = FakeClock()
    fake = FakeOS()
    sup = main.Supervisor(services, fake.spawn, fake.kill, clock, grace)
    return sup, fake, clock


class StatusAndPolicyTests(unittest.TestCase):
    def test_decode_status_distinguishes_exit_codes_from_signals(self):
        self.assertEqual(main.decode_status(exited(0)), ("exited", 0))
        self.assertEqual(main.decode_status(exited(3)), ("exited", 3))
        self.assertEqual(main.decode_status(signaled(signal.SIGTERM)), ("signaled", int(signal.SIGTERM)))

    def test_backoff_doubles_and_caps(self):
        self.assertEqual([main.backoff(i) for i in range(6)], [1, 2, 4, 8, 16, 30])

    def test_should_restart_follows_policy_and_limits(self):
        always = main.Service("a", ["x"], "always", 3)
        on_failure = main.Service("b", ["x"], "on-failure", 3)
        never = main.Service("c", ["x"], "never", 3)
        self.assertTrue(main.should_restart(always, "exited", 0, 0, False))
        self.assertFalse(main.should_restart(on_failure, "exited", 0, 0, False))
        self.assertTrue(main.should_restart(on_failure, "exited", 1, 0, False))
        self.assertTrue(main.should_restart(on_failure, "signaled", 9, 0, False))
        self.assertFalse(main.should_restart(never, "exited", 1, 0, False))
        self.assertFalse(main.should_restart(always, "exited", 1, 3, False))
        self.assertFalse(main.should_restart(always, "exited", 1, 0, True))


class SupervisorTests(unittest.TestCase):
    def test_start_all_spawns_every_service(self):
        sup, fake, _ = make([main.Service("web", ["sleep", "5"]), main.Service("job", ["false"], "never")])
        sup.start_all()
        self.assertEqual([cmd for _, cmd in fake.spawned], [["sleep", "5"], ["false"]])
        self.assertEqual(set(sup.children), {1001, 1002})

    def test_failed_service_is_restarted_after_backoff(self):
        sup, fake, clock = make([main.Service("web", ["srv"], "on-failure", 2)])
        sup.start_all()
        sup.handle_exit(1001, exited(1))
        self.assertEqual(len(sup.pending), 1)
        sup.tick()
        self.assertEqual(len(fake.spawned), 1, "not restarted before the backoff elapsed")
        clock.t += 1
        sup.tick()
        self.assertEqual(len(fake.spawned), 2)
        self.assertEqual(sup.children[1002].restarts, 1)
        sup.handle_exit(1002, exited(1))
        clock.t += 2
        sup.tick()
        self.assertEqual(len(fake.spawned), 3)
        sup.handle_exit(1003, exited(1))
        clock.t += 60
        sup.tick()
        self.assertEqual(len(fake.spawned), 3, "max_restarts reached")
        self.assertTrue(sup.finished)
        self.assertEqual(sup.last_exit_code, 1)

    def test_clean_exit_with_on_failure_is_not_restarted(self):
        sup, fake, _ = make([main.Service("job", ["task"], "on-failure")])
        sup.start_all()
        sup.handle_exit(1001, exited(0))
        self.assertEqual(sup.pending, [])
        self.assertTrue(sup.finished)
        self.assertEqual(sup.last_exit_code, 0)

    def test_adopted_orphans_are_reaped_and_counted(self):
        sup, _, _ = make([main.Service("web", ["srv"])])
        sup.start_all()
        sup.handle_exit(4242, exited(0))
        self.assertEqual(sup.reaped_orphans, 1)
        self.assertIn(1001, sup.children)

    def test_shutdown_forwards_signal_then_escalates_to_sigkill(self):
        sup, fake, clock = make([main.Service("web", ["srv"], "always"), main.Service("db", ["pg"], "always")], grace=5)
        sup.start_all()
        sup.begin_shutdown(signal.SIGTERM)
        self.assertEqual(sorted(fake.killed), [(1001, signal.SIGTERM), (1002, signal.SIGTERM)])
        sup.handle_exit(1001, signaled(signal.SIGTERM))
        self.assertEqual(sup.pending, [], "no restarts while shutting down")
        clock.t += 4
        sup.tick()
        self.assertNotIn((1002, signal.SIGKILL), fake.killed)
        clock.t += 2
        sup.tick()
        self.assertIn((1002, signal.SIGKILL), fake.killed)
        self.assertEqual(sup.last_exit_code, 128 + int(signal.SIGTERM))

    def test_summary_lists_exits(self):
        sup, _, _ = make([main.Service("job", ["task"], "never")])
        sup.start_all()
        sup.handle_exit(1001, exited(2))
        text = sup.summary()
        self.assertIn("services: 1  exits: 1  orphans reaped: 0", text)
        self.assertIn("job      pid=1001   exited 2", text)


class SpecTests(unittest.TestCase):
    def test_parse_spec_from_run_flags_and_validation(self):
        services = main.parse_spec(None, ["sleep 5", "sh -c 'exit 3'"], "always")
        self.assertEqual([s.command for s in services], [["sleep", "5"], ["sh", "-c", "exit 3"]])
        self.assertTrue(all(s.restart == "always" for s in services))
        with self.assertRaises(ValueError):
            main.parse_spec(None, [], "never")
        with self.assertRaises(ValueError):
            main.parse_spec(None, ["sleep 1"], "sometimes")


if __name__ == "__main__":
    unittest.main()
