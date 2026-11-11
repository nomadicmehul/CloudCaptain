import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

CODE = os.path.join(os.path.dirname(__file__), "..")
FIXTURES = os.path.join(CODE, "fixtures")
STRICT = "#!/usr/bin/env bash\nset -euo pipefail\n"


def ids(text):
    return [(f.line, f.check) for f in main.lint_text(text)]


def strict_ids(body):
    return ids(STRICT + body)


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class TokenizerTests(unittest.TestCase):
    def test_word_parts_keep_quoting(self):
        script = main.parse("echo \"a $b\" 'c $d' $e ${f:-x} $(g $h)\n")
        words = script.commands[0].words
        self.assertEqual([w.raw for w in words], ["echo", '"a $b"', "'c $d'", "$e", "${f:-x}", "$(g $h)"])
        self.assertEqual([(p.kind, p.quoted) for p in words[1].parts], [("lit", True), ("param", True)])
        self.assertEqual([(p.kind, p.quoted) for p in words[2].parts], [("sq", True)])
        self.assertEqual((words[3].parts[0].kind, words[3].parts[0].quoted, words[3].parts[0].name), ("param", False, "e"))
        self.assertEqual((words[4].parts[0].name, words[4].parts[0].op), ("f", ":-x"))
        self.assertEqual(words[5].parts[0].kind, "cmdsub")

    def test_command_substitution_is_parsed_as_commands(self):
        script = main.parse('out="$(basename $path)"\n')
        nested = [c for c in script.commands if c.nested]
        self.assertEqual([c.name() for c in nested], ["basename"])
        self.assertEqual(strict_ids('out="$(basename $path)"\necho "$out"\n'), [(3, "BL001")])

    def test_heredoc_body_is_not_code_and_lines_stay_right(self):
        text = STRICT + "cat <<EOF\n$unquoted `cmd` $(ls)\nEOF\necho $after\n"
        self.assertEqual(ids(text), [(6, "BL001")])
        script = main.parse(text)
        cat = [c for c in script.commands if c.name() == "cat"][0]
        self.assertEqual(cat.redirects[0].body, "$unquoted `cmd` $(ls)")

    def test_unterminated_heredoc_and_quote_are_errors(self):
        with self.assertRaises(main.LexError):
            main.parse("cat <<EOF\nnever closed\n")
        with self.assertRaises(main.LexError) as ctx:
            main.parse("echo 'open\n")
        self.assertEqual(ctx.exception.line, 1)

    def test_double_brackets_do_not_split_and_may_hold_regex(self):
        self.assertEqual(strict_ids('if [[ $x =~ ^(a|b)$ && -n $y ]]; then echo "$x"; fi\n'), [])

    def test_functions_case_and_line_continuation(self):
        text = STRICT + 'deploy() {\n  echo "$1"\n}\nfunction rollback {\n  echo "$1"\n}\ncase $mode in\n  a|b) echo $mode ;;\n  *) echo \\\n    $other ;;\nesac\n'
        script = main.parse(text)
        self.assertEqual(script.functions, {"deploy": 3, "rollback": 6})
        self.assertEqual(ids(text), [(10, "BL001"), (12, "BL001")])

    def test_arrays_arithmetic_and_process_substitution(self):
        body = 'args=("$@")\n(( count = ${#args[@]} + 1 ))\necho $(( count * 2 ))\ndiff <(sort a) <(sort b)\nprintf "%s\\n" "${args[@]}"\n'
        self.assertEqual(strict_ids(body), [])


class CheckTests(unittest.TestCase):
    def test_unquoted_expansion_skips_safe_contexts(self):
        body = 'x=$1\nlocal_copy=$x\nexport Y=$x\necho $# $? ${#x}\n$x --version\nfor w in $x; do echo "$w"; done\necho $x\n'
        self.assertEqual(strict_ids(body), [(9, "BL001")])

    def test_constant_literal_variables_are_not_flagged(self):
        self.assertEqual(strict_ids("mode=0644\nchmod $mode f\n"), [])
        self.assertEqual(strict_ids('mode="a b"\nchmod $mode f\n'), [(4, "BL001")])
        self.assertEqual(strict_ids("mode=0644\nmode=$1\nchmod $mode f\n"), [(5, "BL001")])

    def test_for_over_ls_but_not_other_substitutions(self):
        self.assertEqual(strict_ids('for f in $(ls *.txt); do echo "$f"; done\n'), [(3, "BL002")])
        self.assertEqual(strict_ids('for i in $(seq 3); do echo "$i"; done\n'), [])
        self.assertEqual(strict_ids("mv $(ls) /tmp\n"), [(3, "BL012")])

    def test_cd_without_exit_respects_errexit_and_conditions(self):
        plain = "#!/bin/bash\n"
        self.assertIn((2, "BL003"), ids(plain + "cd /srv\n"))
        self.assertNotIn("BL003", [c for _, c in ids(plain + "cd /srv || exit 1\ncd /srv && ls\nif cd /srv; then ls; fi\n")])
        self.assertNotIn("BL003", [c for _, c in strict_ids("cd /srv\n")])
        finding = [f for f in main.lint_text(plain + "f() {\n  cd /srv\n}\n") if f.check == "BL003"][0]
        self.assertIn("|| return", finding.message)

    def test_strict_mode_variants(self):
        for header in ("set -euo pipefail", "set -Eeuo pipefail", "set -o errexit -o nounset -o pipefail", "set -eu\nset -o pipefail"):
            self.assertEqual(ids(f"#!/bin/bash\n{header}\n"), [], header)
        finding = main.lint_text("#!/bin/bash\nset -e\n")[0]
        self.assertEqual((finding.line, finding.check), (1, "BL004"))
        self.assertIn("nounset, pipefail", finding.message)
        self.assertEqual(ids("#!/bin/bash -e\nset -uo pipefail\n"), [])

    def test_test_operands_star_at_and_backticks(self):
        self.assertEqual(strict_ids("[ $v = x ]\ntest -n $v\n"), [(3, "BL005"), (4, "BL005")])
        self.assertEqual(strict_ids("logger $*\nprintf '%s\\n' $@\necho \"$*\" \"$@\"\n"), [(3, "BL010"), (4, "BL011")])
        self.assertEqual(strict_ids('now="`date`"\n'), [(3, "BL009")])

    def test_useless_cat_and_read(self):
        self.assertEqual(strict_ids('cat "$f" | grep x\n'), [(3, "BL006")])
        self.assertEqual(strict_ids('cat -n "$f" | grep x\ncat a b | grep x\ncat <<EOF | grep x\nhi\nEOF\n'), [])
        self.assertEqual(strict_ids("read name\nread -p 'go? ' answer\n"), [(3, "BL007"), (4, "BL007")])
        self.assertEqual(strict_ids("read -r name\nread -rp 'go? ' answer\nwhile IFS= read -r l; do :; done < f\n"), [])

    def test_rm_with_possibly_empty_variable(self):
        self.assertEqual(strict_ids('rm -rf "$dir/"*\nrm -r "$dir"/*\nrm --recursive "$dir/"\n'), [(3, "BL008"), (4, "BL008"), (5, "BL008")])
        self.assertEqual(strict_ids('rm -rf "${dir:?}/"*\nrm -f "$dir/"*\nrm -rf "$dir/"*.log\n'), [])

    def test_mktemp_needs_an_exit_trap(self):
        self.assertEqual(strict_ids('tmp="$(mktemp -d)"\necho "$tmp"\n'), [(3, "BL013")])
        self.assertEqual(strict_ids('trap \'rm -rf "$tmp"\' EXIT\ntmp="$(mktemp -d)"\n'), [])
        self.assertEqual(strict_ids('cleanup() { rm -f "$t"; }\ntrap cleanup EXIT INT\nt="$(mktemp)"\n'), [])

    def test_idioms_shellcheck_accepts_are_accepted(self):
        body = 'SUDO=""\n[[ $(id -u) == 0 ]] || SUDO=sudo\n$SUDO true\ntimeout 5 $SUDO true\nkubectl ${CTX[@]+"${CTX[@]}"} get pods ${NS:+-n "$NS"}\necho $(seq 1 3)\ncat "$d"/*.txt | sort\n'
        self.assertEqual(strict_ids(body), [])
        self.assertEqual(strict_ids('EMPTY=""\necho $EMPTY\n'), [(4, "BL001")])

    def test_disable_directives_for_a_line_or_the_whole_file(self):
        body = "# shellcheck disable=SC2046\nenv $(cat a.env) true\nenv $(cat b.env) true\n# bashlint disable=BL001\necho $x\n"
        self.assertEqual(strict_ids(body), [(5, "BL012")])
        whole = "#!/bin/bash\n# shellcheck disable=SC2086\nset -euo pipefail\necho $a\n[ $b = c ]\n"
        self.assertEqual(ids(whole), [])

    def test_every_check_maps_to_a_shellcheck_code_or_says_none(self):
        self.assertEqual(len({c.id for c in main.CHECKS}), len(main.CHECKS))
        for check in main.CHECKS:
            self.assertRegex(check.shellcheck or "SC0000", r"^SC\d{4}$")
        self.assertEqual({c.id for c in main.CHECKS if not c.shellcheck}, {"BL004", "BL013"})


class FixtureTests(unittest.TestCase):
    def test_bad_fixtures_report_exactly_the_planted_problems(self):
        with open(os.path.join(FIXTURES, "expected.json")) as fh:
            expected = json.load(fh)
        planted = set()
        for name, items in expected.items():
            with open(os.path.join(FIXTURES, "bad", name)) as fh:
                found = [(f.line, f.col, f.check) for f in main.lint_text(fh.read(), name)]
            self.assertEqual(found, [(i["line"], i["column"], i["id"]) for i in items], name)
            planted.update(i["id"] for i in items)
        self.assertEqual(planted, set(main.CHECKS_BY_ID))

    def test_fixed_fixtures_and_the_template_are_clean(self):
        for name in sorted(os.listdir(os.path.join(FIXTURES, "good"))) + ["../../main.sh"]:
            path = os.path.join(FIXTURES, "good", name)
            with open(path) as fh:
                self.assertEqual(main.lint_text(fh.read(), name), [], name)


class CliTests(unittest.TestCase):
    def test_exit_codes_and_json(self):
        code, out, err = run_cli(["check", "--json", os.path.join(FIXTURES, "bad")])
        self.assertEqual(code, 1)
        rows = json.loads(out)
        with open(os.path.join(FIXTURES, "expected.json")) as fh:
            expected = json.load(fh)
        self.assertEqual(len(rows), sum(len(v) for v in expected.values()))
        self.assertEqual({r["shellcheck"] for r in rows if r["id"] == "BL001"}, {"SC2086"})
        self.assertIn("finding(s) in 4 file(s)", err)
        code, out, _ = run_cli(["check", os.path.join(FIXTURES, "good")])
        self.assertEqual((code, out), (0, ""))

    def test_errors_exit_two_and_go_to_stderr(self):
        code, out, err = run_cli(["check", "/no/such/file.sh"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("/no/such/file.sh", err)
        code, _, err = run_cli(["check", "--disable", "BL999", FIXTURES])
        self.assertEqual(code, 2)
        self.assertIn("BL999", err)
        with tempfile.TemporaryDirectory() as tmp:
            broken = os.path.join(tmp, "broken.sh")
            with open(broken, "w") as fh:
                fh.write("echo 'never closed\n")
            code, _, err = run_cli(["check", broken])
            self.assertEqual(code, 2)
            self.assertIn("unterminated single quote", err)

    def test_disable_and_severity_filters(self):
        bad = os.path.join(FIXTURES, "bad", "deploy.sh")
        code, out, _ = run_cli(["check", "--json", "--disable", "BL001,bl009", bad])
        self.assertNotIn("BL001", {r["id"] for r in json.loads(out)})
        self.assertNotIn("BL009", {r["id"] for r in json.loads(out)})
        code, out, _ = run_cli(["check", "--json", "--severity", "error", bad])
        self.assertEqual({r["id"] for r in json.loads(out)}, {"BL002"})

    def test_checks_and_explain(self):
        code, out, _ = run_cli(["checks", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual({c["id"]: c["shellcheck"] for c in json.loads(out)}["BL008"], "SC2115")
        code, out, _ = run_cli(["explain", "--json", os.path.join(FIXTURES, "good", "report.sh")])
        data = json.loads(out)
        self.assertEqual(data["functions"], {"probe": 6})
        kinds = {c["kind"] for c in data["commands"]}
        self.assertTrue({"simple", "for", "case", "dbracket"} <= kinds, kinds)


@unittest.skipUnless(platform.system() == "Linux" and shutil.which("bash"), "main.sh is written for Linux")
class TemplateScriptTests(unittest.TestCase):
    def run_sh(self, *args):
        return subprocess.run(["bash", os.path.join(CODE, "main.sh"), *args], capture_output=True, text=True, timeout=60)

    def test_by_hand_checks_are_a_subset_of_bashlint(self):
        bad = sorted(os.path.join(FIXTURES, "bad", n) for n in os.listdir(os.path.join(FIXTURES, "bad")))
        result = self.run_sh(*bad)
        self.assertEqual(result.returncode, 1, result.stderr)
        by_hand = {(line.split(":")[0], int(line.split(":")[1]), line.split()[1]) for line in result.stdout.splitlines()}
        linted = set()
        for path in bad:
            with open(path) as fh:
                linted.update((path, f.line, f.check) for f in main.lint_text(fh.read(), path))
        self.assertTrue(by_hand, result.stdout)
        self.assertLessEqual(by_hand, linted)

    def test_clean_run_usage_error_and_atomic_report(self):
        good = os.path.join(FIXTURES, "good")
        result = self.run_sh(*sorted(os.path.join(good, n) for n in os.listdir(good)))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        result = self.run_sh("--bogus")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown option", result.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            report = os.path.join(tmp, "report.txt")
            result = self.run_sh("-q", "-o", report, os.path.join(FIXTURES, "bad", "deploy.sh"))
            self.assertEqual((result.returncode, result.stdout), (1, ""))
            self.assertEqual(os.listdir(tmp), ["report.txt"])
            with open(report) as fh:
                self.assertIn("BL002", fh.read())


if __name__ == "__main__":
    unittest.main()
