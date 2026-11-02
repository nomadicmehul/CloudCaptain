import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures")
RULES = os.path.join(os.path.dirname(__file__), "..", "rules", "minifalco_rules.json")


def ev(**fields):
    return main.Event(dict(fields))


def predicate(condition, macros=None, lists=None):
    return main.compile_condition(condition, macros or {}, lists or {})


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TokenizerTests(unittest.TestCase):
    def test_paths_and_operators_tokenize_as_separate_words(self):
        self.assertEqual(
            main.tokenize("fd.name startswith /etc/ and user.uid = 0"),
            ["fd.name", "startswith", "/etc/", "and", "user.uid", "=", "0"],
        )

    def test_quoted_strings_keep_spaces_and_comparison_glued_operators_split(self):
        self.assertEqual(main.tokenize('proc.cmdline contains "rm -rf"'), ["proc.cmdline", "contains", '"rm -rf"'])
        self.assertEqual(main.tokenize("user.uid!=0"), ["user.uid", "!=", "0"])
        self.assertEqual(main.tokenize("fd.sport>=1024"), ["fd.sport", ">=", "1024"])

    def test_unterminated_string_is_a_rule_error(self):
        with self.assertRaises(main.RuleError):
            main.tokenize('proc.name = "oops')


class OperatorTests(unittest.TestCase):
    def test_equality_inequality_and_missing_field(self):
        self.assertTrue(predicate("proc.name = bash").evaluate(ev(**{"proc.name": "bash"})))
        self.assertFalse(predicate("proc.name = bash").evaluate(ev(**{"proc.name": "sh"})))
        self.assertTrue(predicate("proc.name != bash").evaluate(ev(**{"proc.name": "sh"})))
        self.assertTrue(predicate("container.id != host").evaluate(ev(**{"container.id": "abc"})))
        self.assertFalse(predicate("container.id != host").evaluate(ev()))

    def test_string_operators(self):
        self.assertTrue(predicate("fd.name startswith /etc/").evaluate(ev(**{"fd.name": "/etc/shadow"})))
        self.assertTrue(predicate("fd.name endswith .sh").evaluate(ev(**{"fd.name": "/tmp/x.sh"})))
        self.assertTrue(predicate("proc.cmdline contains rm").evaluate(ev(**{"proc.cmdline": "sh -c rm"})))
        self.assertTrue(predicate("fd.name glob /home/*/.ssh/*").evaluate(ev(**{"fd.name": "/home/jo/.ssh/id_rsa"})))
        self.assertFalse(predicate("fd.name glob /home/*/.ssh/*").evaluate(ev(**{"fd.name": "/home/jo/notes"})))

    def test_numeric_comparisons_and_non_numeric_is_false(self):
        self.assertTrue(predicate("user.uid = 0").evaluate(ev(**{"user.uid": 0})))
        self.assertTrue(predicate("fd.sport > 1023").evaluate(ev(**{"fd.sport": 4444})))
        self.assertFalse(predicate("fd.sport > 1023").evaluate(ev(**{"fd.sport": 80})))
        self.assertFalse(predicate("user.uid > 0").evaluate(ev(**{"user.uid": "root"})))

    def test_in_pmatch_and_exists(self):
        self.assertTrue(predicate("proc.name in (bash, sh, zsh)").evaluate(ev(**{"proc.name": "zsh"})))
        self.assertFalse(predicate("proc.name in (bash, sh, zsh)").evaluate(ev(**{"proc.name": "python3"})))
        self.assertTrue(predicate("fd.name pmatch (/etc, /var/run)").evaluate(ev(**{"fd.name": "/etc/passwd"})))
        self.assertFalse(predicate("fd.name pmatch (/etc, /var/run)").evaluate(ev(**{"fd.name": "/etcd/data"})))
        self.assertTrue(predicate("fd.sip exists").evaluate(ev(**{"fd.sip": "10.0.0.1"})))
        self.assertFalse(predicate("fd.sip exists").evaluate(ev()))

    def test_boolean_field_equality_is_case_insensitive(self):
        self.assertTrue(predicate("evt.is_open_write = true").evaluate(ev(**{"evt.is_open_write": True})))
        self.assertFalse(predicate("evt.is_open_write = true").evaluate(ev(**{"evt.is_open_write": False})))


class ParserTests(unittest.TestCase):
    def test_and_or_not_precedence(self):
        node = predicate("a = 1 and b = 2 or c = 3")
        self.assertIsInstance(node, main.Or)
        self.assertTrue(node.evaluate(ev(a="1", b="2")))
        self.assertTrue(node.evaluate(ev(c="3")))
        self.assertFalse(node.evaluate(ev(a="1", c="9")))

    def test_parentheses_group_and_not_binds_one_term(self):
        node = predicate("a = 1 and (b = 2 or c = 3)")
        self.assertTrue(node.evaluate(ev(a="1", c="3")))
        self.assertFalse(node.evaluate(ev(a="1")))
        self.assertTrue(predicate("not proc.name = bash").evaluate(ev(**{"proc.name": "sh"})))

    def test_macros_and_lists_expand(self):
        lists = {"shells": ["bash", "sh"]}
        macros = {"spawned": main.compile_condition("evt.type = execve", {}, lists)}
        node = main.compile_condition("spawned and proc.name in (shells)", macros, lists)
        self.assertTrue(node.evaluate(ev(**{"evt.type": "execve", "proc.name": "bash"})))
        self.assertFalse(node.evaluate(ev(**{"evt.type": "openat", "proc.name": "bash"})))

    def test_unknown_bare_word_and_trailing_token_are_errors(self):
        with self.assertRaises(main.RuleError):
            predicate("proc.name andthen")
        with self.assertRaises(main.RuleError):
            predicate("proc.name = bash extra")


class EventFieldTests(unittest.TestCase):
    def test_flat_and_nested_keys_both_resolve(self):
        self.assertEqual(ev(**{"proc.name": "sh"}).get("proc.name"), "sh")
        nested = main.Event({"container": {"image": {"repository": "nginx"}}})
        self.assertEqual(nested.get("container.image.repository"), "nginx")

    def test_container_id_defaults_to_host_and_index_access(self):
        self.assertEqual(ev().get("container.id"), "host")
        self.assertEqual(main.Event({"proc.aname": ["cat", "bash", "runc"]}).get("proc.aname[1]"), "bash")
        self.assertIsNone(main.Event({"proc.aname": ["cat"]}).get("proc.aname[5]"))


class StraceParseTests(unittest.TestCase):
    def test_execve_sets_proc_name_and_parent_from_clone(self):
        text = (
            '[pid 2000] 12:00:01.0 execve("/bin/bash", ["bash"], 0x0 /* 2 vars */) = 0\n'
            '[pid 2000] 12:00:02.0 clone(child_stack=NULL, flags=SIGCHLD) = 2001\n'
            '[pid 2001] 12:00:02.1 execve("/bin/cat", ["cat", "/etc/shadow"], 0x0 /* 2 vars */) = 0\n'
        )
        events = main.parse_strace_events(text, {"container.id": "c1"})
        execs = [e for e in events if e.get("evt.type") == "execve"]
        self.assertEqual(execs[0].get("proc.name"), "bash")
        self.assertEqual(execs[1].get("proc.name"), "cat")
        self.assertEqual(execs[1].get("proc.pname"), "bash")
        self.assertEqual(execs[1].get("container.id"), "c1")

    def test_open_flags_decide_read_versus_write(self):
        text = (
            '12:00:01.0 execve("/bin/touch", ["touch", "/etc/x"], 0x0 /* 1 vars */) = 0\n'
            '12:00:02.0 openat(AT_FDCWD, "/etc/x", O_WRONLY|O_CREAT, 0644) = 3\n'
            '12:00:03.0 openat(AT_FDCWD, "/etc/shadow", O_RDONLY) = 4\n'
        )
        events = main.parse_strace_events(text, {})
        writes = [e for e in events if e.get("evt.type") == "openat" and e.get("evt.is_open_write")]
        reads = [e for e in events if e.get("evt.type") == "openat" and e.get("evt.is_open_read") and not e.get("evt.is_open_write")]
        self.assertEqual(writes[0].get("fd.name"), "/etc/x")
        self.assertEqual(reads[0].get("fd.name"), "/etc/shadow")

    def test_connect_parses_inet_and_unix_addresses(self):
        text = (
            '12:00:01.0 connect(3, {sa_family=AF_INET, sin_port=htons(4444), sin_addr=inet_addr("203.0.113.10")}, 16) = 0\n'
            '12:00:02.0 connect(4, {sa_family=AF_UNIX, sun_path="/var/run/docker.sock"}, 110) = 0\n'
        )
        events = main.parse_strace_events(text, {})
        self.assertEqual(events[0].get("fd.sip"), "203.0.113.10")
        self.assertEqual(events[0].get("fd.sport"), 4444)
        self.assertEqual(events[1].get("fd.name"), "/var/run/docker.sock")

    def test_failed_and_resumed_lines_are_ignored(self):
        text = (
            '12:00:01.0 openat(AT_FDCWD, "/missing", O_RDONLY) = -1 ENOENT (No such file or directory)\n'
            '12:00:02.0 <... futex resumed>) = 0\n'
            '12:00:03.0 +++ exited with 0 +++\n'
        )
        events = main.parse_strace_events(text, {})
        self.assertEqual(events, [])


class RulesetTests(unittest.TestCase):
    def setUp(self):
        self.ruleset = main.load_ruleset([RULES])

    def test_default_ruleset_fires_one_alert_per_attack_event(self):
        with open(os.path.join(FIX, "events-attack.json"), encoding="utf-8") as handle:
            events = main.load_json_events(handle.read(), "attack", {})
        alerts = main.evaluate(self.ruleset, events)
        names = sorted(a.rule for a in alerts)
        self.assertEqual(names, sorted({
            "Shell spawned in a container",
            "Read sensitive file untrusted",
            "Write below etc",
            "Execution from world-writable directory",
            "Unexpected outbound connection",
            "Container runtime socket accessed",
        }))

    def test_benign_events_raise_nothing(self):
        with open(os.path.join(FIX, "events-benign.json"), encoding="utf-8") as handle:
            events = main.load_json_events(handle.read(), "benign", {})
        self.assertEqual(main.evaluate(self.ruleset, events), [])

    def test_strace_capture_matches_the_json_fixture_rules(self):
        with open(os.path.join(FIX, "capture-attack.strace"), encoding="utf-8") as handle:
            events = main.parse_strace_events(handle.read(), {"container.id": "a1b2c3d4e5f6", "user.name": "root"})
        alerts = main.evaluate(self.ruleset, events)
        self.assertEqual(
            sorted(a.rule for a in alerts),
            sorted({
                "Shell spawned in a container",
                "Read sensitive file untrusted",
                "Write below etc",
                "Execution from world-writable directory",
                "Unexpected outbound connection",
                "Container runtime socket accessed",
            }),
        )

    def test_shell_outside_a_container_does_not_fire(self):
        node = [r for r in self.ruleset.rules if r.name == "Shell spawned in a container"][0]
        self.assertFalse(node.matches(ev(**{"evt.type": "execve", "proc.name": "bash", "container.id": "host"})))
        self.assertTrue(node.matches(ev(**{"evt.type": "execve", "proc.name": "bash", "container.id": "abc"})))

    def test_min_priority_filters_by_severity(self):
        with open(os.path.join(FIX, "events-attack.json"), encoding="utf-8") as handle:
            events = main.load_json_events(handle.read(), "attack", {})
        severe = main.evaluate(self.ruleset, events, min_priority="ERROR")
        self.assertEqual(sorted(a.rule for a in severe), ["Container runtime socket accessed", "Write below etc"])


class ExceptionAndTuneTests(unittest.TestCase):
    def test_exception_suppresses_a_match(self):
        data = {
            "lists": {"pkg": ["apt", "dpkg"]},
            "rules": [{
                "rule": "Write below etc",
                "condition": "fd.name startswith /etc/",
                "priority": "ERROR",
                "exceptions": [{"field": "proc.name", "op": "in", "values": ["pkg"]}],
            }],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(data, handle)
            path = handle.name
        try:
            ruleset = main.load_ruleset([path])
            rule = ruleset.rules[0]
            self.assertFalse(rule.matches(ev(**{"fd.name": "/etc/apt/sources.list", "proc.name": "apt"})))
            self.assertTrue(rule.matches(ev(**{"fd.name": "/etc/cron.d/x", "proc.name": "touch"})))
        finally:
            os.unlink(path)

    def test_tune_counts_matches_and_top_processes(self):
        ruleset = main.load_ruleset([RULES])
        events = [
            ev(**{"evt.type": "openat", "evt.is_open_write": True, "fd.name": "/etc/a", "proc.name": "touch", "container.id": "c"}),
            ev(**{"evt.type": "openat", "evt.is_open_write": True, "fd.name": "/etc/b", "proc.name": "touch", "container.id": "c"}),
            ev(**{"evt.type": "openat", "evt.is_open_write": True, "fd.name": "/etc/c", "proc.name": "sed", "container.id": "c"}),
        ]
        report = main.tune(ruleset, events)
        write = [row for row in report if row["rule"] == "Write below etc"][0]
        self.assertEqual(write["matches"], 3)
        self.assertEqual(write["top_processes"][0], {"proc.name": "touch", "count": 2})


class OutputAndCliTests(unittest.TestCase):
    def test_output_substitution_uses_na_for_missing_fields(self):
        rendered, used = main.render_output("proc=%proc.name parent=%proc.pname", ev(**{"proc.name": "bash"}))
        self.assertEqual(rendered, "proc=bash parent=<NA>")
        self.assertIn("proc.name", used)

    def test_run_exit_code_is_one_when_alerts_fire_zero_when_clean(self):
        code, out, _ = run_cli("run", os.path.join(FIX, "events-attack.json"))
        self.assertEqual(code, 1)
        self.assertIn("6 alert(s)", out)
        code, out, _ = run_cli("run", os.path.join(FIX, "events-benign.json"))
        self.assertEqual(code, 0)
        self.assertIn("0 alert(s)", out)

    def test_run_json_output_is_parseable(self):
        code, out, _ = run_cli("run", os.path.join(FIX, "capture-attack.strace"), "--container-id", "abc", "--json")
        payload = json.loads(out)
        self.assertEqual(len(payload["alerts"]), 6)
        self.assertTrue(all("priority" in a for a in payload["alerts"]))

    def test_list_rules_json_lists_every_rule(self):
        code, out, _ = run_cli("list-rules", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["rules"]), 6)

    def test_bad_rules_file_and_missing_events_exit_two(self):
        code, _, err = run_cli("run", "/no/such/events.json")
        self.assertEqual(code, 2)
        self.assertIn("not found", err)


if __name__ == "__main__":
    unittest.main()
