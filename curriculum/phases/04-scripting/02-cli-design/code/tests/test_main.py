import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cliforge  # noqa: E402
import main  # noqa: E402

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"
GOLDEN = HERE / "golden"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"
ESC = "\033["


class Stream(StringIO):
    def __init__(self, text="", tty=False):
        super().__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


class BrokenStdout(Stream):
    def write(self, text):
        raise BrokenPipeError(32, "Broken pipe")

    def fileno(self):
        raise OSError("no file descriptor")


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.inventory = os.path.join(self.tmp, "inventory.json")
        shutil.copy(FIXTURES / "inventory.json", self.inventory)
        self.env = {"HOME": self.tmp, "XDG_CONFIG_HOME": os.path.join(self.tmp, "xdg"), "CCOPS_INVENTORY": self.inventory}

    def run_cli(self, *argv, env=None, stdin="", tty_in=False, tty_out=False, tty_err=False):
        environ = dict(self.env)
        environ.update(env or {})
        out, err = Stream(tty=tty_out), Stream(tty=tty_err)
        console = cliforge.Console(Stream(stdin, tty=tty_in), out, err, environ, prog="ccops")
        code = main.main(list(argv), console)
        return code, out.getvalue(), err.getvalue()

    def write_config(self, text, name="config"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def inventory_bytes(self):
        with open(self.inventory, "rb") as handle:
            return handle.read()

    def assert_golden(self, name, text):
        path = GOLDEN / name
        if UPDATE:
            GOLDEN.mkdir(exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.assertTrue(path.exists(), f"missing golden file {path}; run with UPDATE_GOLDEN=1 once and review the diff")
        self.assertEqual(path.read_text(encoding="utf-8"), text, f"{name} changed; if intended, rerun with UPDATE_GOLDEN=1 and review the diff")


class PrecedenceTests(CliTestCase):
    def explain(self, *argv, env=None):
        code, out, err = self.run_cli("config", "--explain", "--json", *argv, env=env)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        return {s["key"]: s for s in data["settings"]}, data

    def test_flag_beats_env_beats_file_beats_default(self):
        config = self.write_config("inventory = other.json\ncolor = never\nlock_timeout = 2\n")
        settings, data = self.explain("--config", config, "--color", "always", env={"CCOPS_INVENTORY": "", "CCOPS_LOCK_TIMEOUT": "3"})
        self.assertEqual((settings["color"]["source"], settings["color"]["value"], settings["color"]["origin"]), ("flag", "always", "--color"))
        self.assertEqual((settings["lock_timeout"]["source"], settings["lock_timeout"]["value"]), ("env", 3))
        self.assertEqual(settings["lock_timeout"]["origin"], "CCOPS_LOCK_TIMEOUT")
        self.assertEqual(settings["inventory"]["source"], "file")
        self.assertEqual(settings["inventory"]["origin"], f"{config}:1")
        self.assertEqual((settings["output"]["source"], settings["output"]["origin"]), ("flag", "--json"))
        self.assertEqual(settings["inventory"]["value"], os.path.join(self.tmp, "other.json"))
        self.assertEqual(data["config_file"], {"path": config, "origin": "--config", "exists": True})

    def test_relative_path_in_config_file_is_relative_to_the_file(self):
        config = self.write_config("inventory = data/hosts.json\n")
        settings, _ = self.explain("--config", config, env={"CCOPS_INVENTORY": ""})
        self.assertEqual(settings["inventory"]["value"], os.path.join(self.tmp, "data", "hosts.json"))

    def test_empty_environment_variable_counts_as_unset(self):
        settings, _ = self.explain(env={"CCOPS_INVENTORY": "", "CCOPS_COLOR": ""})
        self.assertEqual(settings["inventory"]["source"], "default")
        self.assertEqual(settings["color"]["source"], "default")

    def test_config_file_found_through_xdg_and_env(self):
        os.makedirs(os.path.join(self.tmp, "xdg", "ccops"))
        self.write_config("lock_timeout = 7\n", name=os.path.join("xdg", "ccops", "config"))
        settings, data = self.explain()
        self.assertEqual((settings["lock_timeout"]["source"], settings["lock_timeout"]["value"]), ("file", 7))
        self.assertEqual((data["config_file"]["origin"], data["config_file"]["exists"]), ("default", True))
        other = self.write_config("lock_timeout = 9\n", name="elsewhere")
        settings, data = self.explain(env={"CCOPS_CONFIG": other})
        self.assertEqual(settings["lock_timeout"]["value"], 9)
        self.assertEqual(data["config_file"]["origin"], "CCOPS_CONFIG")

    def test_bad_config_file_is_a_usage_error_with_line_and_suggestion(self):
        config = self.write_config("# comment\ninventry = x.json\n")
        code, out, err = self.run_cli("config", "--config", config)
        self.assertEqual(code, cliforge.USAGE)
        self.assertEqual(out, "")
        self.assertIn(f"{config}:2: unknown key 'inventry'; did you mean 'inventory'?", err)
        code, _, err = self.run_cli("config", "--config", os.path.join(self.tmp, "missing"))
        self.assertEqual(code, cliforge.USAGE)
        self.assertIn("does not exist", err)

    def test_bad_values_name_their_source(self):
        code, _, err = self.run_cli("hosts", "list", env={"CCOPS_LOCK_TIMEOUT": "soon"})
        self.assertEqual(code, cliforge.USAGE)
        self.assertIn("invalid lock_timeout 'soon' from CCOPS_LOCK_TIMEOUT", err)
        code, _, err = self.run_cli("hosts", "list", "--output", "yaml")
        self.assertEqual(code, cliforge.USAGE)
        self.assertIn("from --output (choose from text, json)", err)


class OutputTests(CliTestCase):
    def test_list_json_is_one_document_and_stderr_is_empty(self):
        code, out, err = self.run_cli("hosts", "list", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([h["name"] for h in json.loads(out)["hosts"]], ["db-1", "web-1", "web-2", "web-9"])
        code, out, _ = self.run_cli("hosts", "list", "-o", "json", "--env", "prod", "--role", "web")
        self.assertEqual([h["name"] for h in json.loads(out)["hosts"]], ["web-1", "web-2"])

    def test_quiet_prints_names_only_and_notes_go_to_stderr(self):
        code, out, err = self.run_cli("hosts", "list", "--quiet", "--state", "active")
        self.assertEqual((code, out, err), (0, "db-1\nweb-1\n", ""))
        code, out, err = self.run_cli("hosts", "list", "--env", "dev")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("no hosts match", err)
        code, out, err = self.run_cli("hosts", "list", "--env", "dev", "--quiet")
        self.assertEqual((code, out, err), (0, "", ""))

    def test_not_found_is_exit_3_with_a_suggestion_and_no_stdout(self):
        code, out, err = self.run_cli("hosts", "show", "web-3", "--json")
        self.assertEqual((code, out), (cliforge.NOT_FOUND, ""))
        self.assertIn("host 'web-3' not found", err)
        self.assertIn("did you mean", err)

    def test_usage_errors_exit_2_and_say_what_to_run(self):
        cases = [
            (("hosts", "rmove"), "did you mean 'remove'?"),
            (("hosts", "list", "--colour", "never"), "unrecognized arguments: --colour"),
            (("hosts", "list", "--json", "--output", "text"), "--json conflicts with --output text"),
            (("hosts", "list", "--quiet", "--verbose"), "cannot be used together"),
            (("hosts", "add", "web-3", "--env", "prod"), "hosts add needs --role, --ip"),
            (("hosts", "add", "Web_3", "--env", "prod", "--role", "web", "--ip", "10.0.0.300"), "is not an IPv4 or IPv6 address"),
            (("hosts",), "missing command for 'ccops hosts'"),
            ((), "missing command for 'ccops'"),
        ]
        for argv, needle in cases:
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual(code, cliforge.USAGE, err)
                self.assertEqual(out, "")
                self.assertIn(needle, err)
                self.assertTrue(err.startswith("ccops: error: "), err)

    def test_help_and_version_go_to_stdout_with_exit_0(self):
        app = main.build_app()
        for path in app.commands:
            with self.subTest(path=path):
                code, out, err = self.run_cli(*path, "--help")
                self.assertEqual((code, err), (0, ""))
                self.assertTrue(out.startswith(app.display(path) + " - "))
        self.assertEqual(self.run_cli("--version"), (0, f"ccops {main.VERSION}\n", ""))

    def test_broken_pipe_and_interrupt_map_to_128_plus_signal(self):
        console = cliforge.Console(Stream(), BrokenStdout(), Stream(), dict(self.env), prog="ccops")
        self.assertEqual(main.main(["hosts", "list"], console), cliforge.BROKEN_PIPE)
        app = cliforge.App("t", "0", "test", settings=[])

        def interrupted(ctx):
            raise KeyboardInterrupt

        app.add(cliforge.Command(("go",), "interrupt", interrupted))
        err = Stream()
        code = app.run(["go"], cliforge.Console(Stream(), Stream(), err, dict(self.env), prog="t"))
        self.assertEqual(code, cliforge.INTERRUPTED)
        self.assertIn("interrupted", err.getvalue())

    def test_inventory_errors_point_at_the_problem(self):
        with open(self.inventory, "w", encoding="utf-8") as handle:
            handle.write('{"hosts": [\n  {"name": }\n]}\n')
        code, out, err = self.run_cli("hosts", "list")
        self.assertEqual((code, out), (cliforge.FAILURE, ""))
        self.assertIn(f"{self.inventory}:2:12: not valid JSON: Expecting value", err)
        with open(self.inventory, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "hosts": [{"name": "a", "env": "x", "role": "y", "ip": "10.0.0.1"}] * 2}, handle)
        code, _, err = self.run_cli("hosts", "list")
        self.assertEqual(code, cliforge.FAILURE)
        self.assertIn("hosts[1]: duplicate name 'a'", err)


class ColourTests(CliTestCase):
    def test_colour_decision_table(self):
        tty, pipe = Stream(tty=True), Stream()
        cases = [
            ("auto", tty, {}, True),
            ("auto", pipe, {}, False),
            ("auto", tty, {"NO_COLOR": "1"}, False),
            ("auto", tty, {"NO_COLOR": ""}, True),
            ("auto", tty, {"TERM": "dumb"}, False),
            ("never", tty, {}, False),
            ("always", pipe, {"NO_COLOR": "1"}, True),
        ]
        for mode, stream, environ, expected in cases:
            with self.subTest(mode=mode, tty=stream.tty, environ=environ):
                self.assertEqual(cliforge.color_decision(mode, stream, environ)[0], expected)

    def test_colour_only_on_a_terminal_without_no_color(self):
        _, out, _ = self.run_cli("hosts", "list", tty_out=True)
        self.assertIn(ESC, out)
        for kwargs in ({"tty_out": False}, {"tty_out": True, "env": {"NO_COLOR": "1"}}, {"tty_out": True, "env": {"CCOPS_COLOR": "never"}}):
            with self.subTest(**kwargs):
                _, out, _ = self.run_cli("hosts", "list", **kwargs)
                self.assertNotIn(ESC, out)
        _, out, _ = self.run_cli("hosts", "list", "--json", "--color", "always")
        self.assertNotIn(ESC, out)
        json.loads(out)


class ChangeTests(CliTestCase):
    ADD = ("hosts", "add", "web-3", "--env", "prod", "--role", "web", "--ip", "10.0.1.13", "--tag", "edge", "--tag", "canary")

    def test_dry_run_reports_and_changes_nothing(self):
        before = self.inventory_bytes()
        code, out, err = self.run_cli(*self.ADD, "--dry-run")
        self.assertEqual((code, out, err), (0, "would add web-3\n", ""))
        code, out, _ = self.run_cli("hosts", "remove", "web-1", "web-2", "-n", "--json")
        self.assertEqual(json.loads(out), {"action": "remove", "changed": True, "dry_run": True, "removed": ["web-1", "web-2"], "missing": []})
        self.assertEqual(self.inventory_bytes(), before)
        self.assertFalse(os.path.exists(self.inventory + ".lock"))

    def test_add_is_idempotent_and_conflicts_fail(self):
        code, out, _ = self.run_cli(*self.ADD, "--json")
        self.assertEqual(code, 0)
        first = json.loads(out)
        self.assertEqual((first["changed"], first["host"]["tags"]), (True, ["canary", "edge"]))
        after_first = self.inventory_bytes()
        code, out, _ = self.run_cli(*self.ADD, "--json")
        self.assertEqual((code, json.loads(out)["changed"]), (0, False))
        self.assertEqual(self.inventory_bytes(), after_first)
        code, out, err = self.run_cli("hosts", "add", "web-3", "--env", "prod", "--role", "web", "--ip", "10.0.1.99")
        self.assertEqual((code, out), (cliforge.FAILURE, ""))
        self.assertIn("ip '10.0.1.13' != '10.0.1.99'", err)
        self.assertEqual([n for n in os.listdir(self.tmp) if n.endswith(".tmp")], [])

    def test_remove_refuses_without_a_terminal(self):
        before = self.inventory_bytes()
        code, out, err = self.run_cli("hosts", "remove", "web-9", stdin="y\n")
        self.assertEqual((code, out), (cliforge.REFUSED, ""))
        self.assertIn("stdin is not a terminal", err)
        self.assertIn("pass --yes", err)
        self.assertEqual(self.inventory_bytes(), before)

    def test_remove_asks_on_a_terminal(self):
        code, out, err = self.run_cli("hosts", "remove", "web-9", stdin="n\n", tty_in=True)
        self.assertEqual((code, out), (cliforge.REFUSED, ""))
        self.assertIn("[y/N]", err)
        code, out, err = self.run_cli("hosts", "remove", "web-9", stdin="yes\n", tty_in=True)
        self.assertEqual((code, out), (0, "removed web-9\n"))
        self.assertEqual([h["name"] for h in json.loads(self.inventory_bytes())["hosts"]], ["db-1", "web-1", "web-2"])

    def test_remove_with_yes_and_missing_hosts(self):
        before = self.inventory_bytes()
        code, out, err = self.run_cli("hosts", "remove", "web-9", "web-7", "--yes")
        self.assertEqual((code, out), (cliforge.NOT_FOUND, ""))
        self.assertIn("web-7; nothing was removed", err)
        self.assertEqual(self.inventory_bytes(), before)
        code, out, err = self.run_cli("hosts", "remove", "web-9", "web-7", "--yes", "--missing-ok", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"action": "remove", "changed": True, "dry_run": False, "removed": ["web-9"], "missing": ["web-7"]})
        code, out, _ = self.run_cli("hosts", "remove", "web-9", "--missing-ok", "-q")
        self.assertEqual((code, out), (0, ""))

    @unittest.skipIf(main.fcntl is None, "flock needs fcntl")
    def test_lock_timeout_fails_with_the_holder_hint(self):
        import fcntl
        with open(self.inventory + ".lock", "a+") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            ticks = iter([0.0, 0.0, 0.5, 1.0, 1.5, 2.5])
            slept = []
            with self.assertRaises(cliforge.CliError) as caught:
                with main.locked(Path(self.inventory), 2.0, clock=lambda: next(ticks), sleep=slept.append):
                    self.fail("lock must not be granted while another process holds it")
            self.assertIn("gave up after 2s", caught.exception.message)
            self.assertIn("lsof", caught.exception.hint)
            self.assertEqual(slept, [main.LOCK_POLL] * 4)
        with main.locked(Path(self.inventory), 0.0) as waited:
            self.assertGreaterEqual(waited, 0.0)


class GeneratedTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.env["CCOPS_INVENTORY"] = str(FIXTURES / "inventory.json")

    def stdout_of(self, *argv, env=None):
        code, out, err = self.run_cli(*argv, env=env)
        self.assertEqual((code, err), (0, ""))
        return out

    def test_golden_help(self):
        self.assert_golden("help.txt", self.stdout_of("--help"))
        self.assert_golden("help-hosts-remove.txt", self.stdout_of("hosts", "remove", "-h"))

    def test_golden_output(self):
        self.assert_golden("hosts-list.txt", self.stdout_of("hosts", "list"))
        self.assert_golden("hosts-list.json", self.stdout_of("hosts", "list", "--json"))
        self.write_config("inventory = inventory.json\ncolor = never\nlock_timeout = 2\n")
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        text = self.stdout_of("config", "--explain", "--config", "config", "--color", "auto", env={"CCOPS_INVENTORY": "", "CCOPS_LOCK_TIMEOUT": "3"})
        self.assert_golden("config-explain.txt", text)

    def test_golden_completion_and_man_page(self):
        self.assert_golden("completion.bash", self.stdout_of("completion", "bash"))
        man = self.stdout_of("man")
        self.assert_golden("ccops.1", man)
        self.assertTrue(man.startswith(".TH CCOPS 1 "))
        self.assertIn("\\-\\-dry\\-run", man)
        for code, _, _ in cliforge.EXIT_CODES:
            self.assertIn(f".B {code}\n", man)

    @unittest.skipIf(shutil.which("bash") is None, "bash not installed")
    def test_completion_in_a_real_bash(self):
        script = self.stdout_of("completion", "bash")
        probe = (
            script + "\n"
            't() { COMP_WORDS=("$@"); COMP_CWORD=$(( ${#COMP_WORDS[@]} - 1 )); _ccops_complete; echo "${COMPREPLY[*]}"; }\n'
            't ccops ""\nt ccops hosts r\nt ccops hosts remove --d\nt ccops --color ""\nt ccops hosts list --state m\n'
        )
        result = subprocess.run(["bash", "--norc", "--noprofile", "-c", probe], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["hosts config completion man", "remove", "--dry-run", "auto always never", "maintenance"])


if __name__ == "__main__":
    unittest.main()
