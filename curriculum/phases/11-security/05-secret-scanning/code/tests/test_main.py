import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import string
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
UPPER32 = string.ascii_uppercase + "234567"
ALNUM = string.ascii_letters + string.digits
C1, C2, C3 = "1a" * 20, "2b" * 20, "3c" * 20


def rand(alphabet, n):
    return "".join(secrets.choice(alphabet) for _ in range(n))


def aws_key_id():
    return "AKIA" + rand(UPPER32, 15) + secrets.choice("234567")


def aws_secret():
    return rand(ALNUM + "/+", 38) + secrets.choice(string.digits) + "/"


def github_pat():
    return "ghp" + "_" + rand(ALNUM, 36)


def slack_bot():
    return "xox" + "b-" + rand(string.digits, 12) + "-" + rand(string.digits, 12) + "-" + rand(ALNUM, 24)


def slack_webhook():
    return "https://hooks.slack" + ".com/services/T" + rand(UPPER32, 8) + "/B" + rand(UPPER32, 8) + "/" + rand(ALNUM, 24)


def pem_lines():
    body = [base64.b64encode(os.urandom(48)).decode() for _ in range(2)]
    return ["-----BEGIN " + "OPENSSH PRIVATE KEY-----"] + body + ["-----END " + "OPENSSH PRIVATE KEY-----"]


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt():
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps({"sub": "cc-test", "iat": 1761955200}).encode())
    signature = b64url(hmac.new(os.urandom(32), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def password():
    return rand(ALNUM + "!#%", 17) + secrets.choice(string.digits) + "!" + secrets.choice(string.ascii_letters)


def scan_text(text, path="app.env", config=None, **kwargs):
    return main.scan([main.Fragment(text, path=path)], config or main.build_config(), **kwargs)


def rules_of(findings):
    return sorted(f.rule for f in findings)


def run(argv, runner=None):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv, runner) if runner else main.main(argv)
    return code, out.getvalue(), err.getvalue()


def fixture_log(values):
    with open(os.path.join(FIXTURES, "git-log-p.txt"), encoding="utf-8") as handle:
        text = handle.read()
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    return text


def fixture_values():
    pem = pem_lines()
    return {
        "AWS_KEY_ID": aws_key_id(),
        "AWS_SECRET": aws_secret(),
        "SLACK_WEBHOOK": slack_webhook(),
        "SLACK_BOT": slack_bot(),
        "PEM_BEGIN": pem[0],
        "PEM_BODY": pem[1],
        "PEM_END": pem[-1],
        "DB_PASSWORD": password(),
    }


def messages_output(records):
    return "".join("\x1f".join(r) + "\n" + "\x00" for r in records)


class FakeGit:
    def __init__(self, log="", messages="", staged="", hooks=".git/hooks"):
        self.outputs = {"log -p": log, "log -z": messages, "diff": staged, "rev-parse": hooks}
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        joined = " ".join(args)
        for key, value in self.outputs.items():
            if f" {key}" in joined:
                return value
        raise AssertionError(f"unexpected git call: {joined}")


class EntropyTests(unittest.TestCase):
    def test_shannon_entropy_is_bits_per_character(self):
        self.assertEqual(main.shannon_entropy(""), 0.0)
        self.assertEqual(main.shannon_entropy("aaaa"), 0.0)
        self.assertAlmostEqual(main.shannon_entropy("abab"), 1.0)
        self.assertAlmostEqual(main.shannon_entropy("abcd"), 2.0)
        self.assertAlmostEqual(main.shannon_entropy("abcdefgh"), 3.0)
        self.assertLess(main.shannon_entropy("hunter2hunter2"), 3.0)
        # A random 40-char key averages ~4.85 bits but dips below 4.5 about 2% of the
        # time; assert the aws-secret-access-key rule's own floor (4.0) instead.
        self.assertGreater(main.shannon_entropy(aws_secret()), 4.0)


class RuleTests(unittest.TestCase):
    def test_provider_rules_find_generated_tokens_at_their_line_and_column(self):
        key, pat, bot, hook = aws_key_id(), github_pat(), slack_bot(), slack_webhook()
        text = f"REGION=eu-central-1\nAWS_ACCESS_KEY_ID={key}\nGH={pat}\nbot: {bot}\nurl = {hook}\n"
        findings, _ = scan_text(text)
        by_rule = {f.rule: f for f in findings}
        self.assertEqual(rules_of(findings), ["aws-access-token", "github-pat", "slack-bot-token", "slack-webhook-url"])
        self.assertEqual((by_rule["aws-access-token"].line, by_rule["aws-access-token"].column), (2, 19))
        self.assertEqual(by_rule["aws-access-token"].secret, key)
        self.assertEqual((by_rule["github-pat"].line, by_rule["github-pat"].column, by_rule["github-pat"].end_column), (3, 4, 43))
        self.assertEqual(by_rule["slack-bot-token"].line, 4)
        self.assertEqual(by_rule["slack-webhook-url"].secret, hook)

    def test_the_aws_documentation_key_is_allowlisted_for_every_rule(self):
        findings, stats = scan_text("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\naws_access_key: AKIAIOSFODNN7EXAMPLE\n")
        self.assertEqual(findings, [])
        self.assertGreaterEqual(stats.suppressed["allowlist"], 2)

    def test_keyword_prefilter_skips_rules_whose_keywords_are_absent(self):
        config = main.build_config({"use_default": False, "rules": [{"id": "word", "regex": "abc[0-9]+", "keywords": ["ZZZ"]}]})
        findings, stats = scan_text("abc123\n", config=config)
        self.assertEqual((findings, stats.prefiltered, stats.rule_runs), ([], 1, 0))
        findings, stats = scan_text("abc123 zzz\n", config=config)
        self.assertEqual((rules_of(findings), stats.prefiltered, stats.rule_runs), (["word"], 0, 1))
        _, stats = scan_text("nothing to see here\n")
        self.assertEqual(stats.prefiltered, len(main.build_config().rules))

    def test_entropy_and_the_identifier_allowlist_separate_values_from_placeholders(self):
        value = password()
        text = "\n".join([
            'weak_password = "hunter2hunter2"',
            'PLACEHOLDER_PASSWORD = "${DB_PASSWORD}"',
            'password = os.environ["DB_PASSWORD"]',
            "token = get_token()",
            "password=$DB_PASSWORD",
            "api_key: your_api_key_here_please",
            f'DB_PASSWORD = "{value}"',
            '"secret_sha256": finding.secret_sha256,',
            'DB_PASSWORD = "@DB_PASSWORD@"',
        ]) + "\n"
        findings, stats = scan_text(text, path="settings.py")
        self.assertEqual([(f.rule, f.line, f.secret) for f in findings], [("generic-password", 7, value)])
        self.assertEqual(stats.suppressed["entropy"], 1)
        self.assertGreaterEqual(stats.suppressed["allowlist"], 4)

    def test_the_committed_fixture_holds_only_placeholders(self):
        stats = main.Stats()
        config = main.build_config()
        findings, _ = main.scan(main.dir_fragments(FIXTURES, config, stats), config, stats=stats)
        self.assertEqual(findings, [])
        self.assertGreater(stats.suppressed["allowlist"], 0)

    def test_a_generic_match_on_the_same_line_as_a_provider_match_is_dropped(self):
        key, secret = aws_key_id(), aws_secret()
        findings, stats = scan_text(f'AWS_ACCESS_KEY_ID={key}\naws_secret_access_key = "{secret}"\n')
        self.assertEqual(rules_of(findings), ["aws-access-token", "aws-secret-access-key"])
        self.assertEqual(stats.suppressed["duplicate"], 2)

    def test_inline_ignore_suppresses_only_its_own_line_and_can_be_disabled(self):
        first, second = github_pat(), github_pat()
        text = f"GH={first}  # gitleaks:allow\nOTHER={second}\n"
        findings, stats = scan_text(text)
        self.assertEqual([f.secret for f in findings], [second])
        self.assertEqual(stats.suppressed["inline"], 1)
        findings, _ = scan_text(text, inline_ignore=False)
        self.assertEqual(len(findings), 2)

    def test_a_private_key_block_is_one_finding_spanning_its_lines(self):
        lines = pem_lines()
        findings, _ = scan_text("# deploy\n" + "\n".join(lines) + "\n", path="deploy/id_ed25519")
        self.assertEqual(len(findings), 1)
        self.assertEqual((findings[0].rule, findings[0].line, findings[0].end_line), ("private-key", 2, 5))
        self.assertTrue(findings[0].secret.startswith(lines[0]) and findings[0].secret.endswith(lines[-1]))

    def test_a_jwt_must_have_a_header_that_decodes_to_json_with_alg(self):
        token = jwt()
        fake = "eyJ" + rand(ALNUM, 30) + ".eyJ" + rand(ALNUM, 30) + "." + rand(ALNUM, 20)
        findings, stats = scan_text(f'session = "{token}"\nfake = "{fake}"\n')
        self.assertEqual([(f.rule, f.line) for f in findings], [("jwt", 1)])
        self.assertEqual(stats.suppressed["invalid"], 1)
        self.assertTrue(main.valid_jwt(token))
        self.assertFalse(main.valid_jwt("eyJ!!!.eyJ.x"))


class GitParsingTests(unittest.TestCase):
    def test_parse_patch_reads_commits_paths_and_the_line_numbers_of_added_lines(self):
        values = fixture_values()
        fragments = main.parse_patch(fixture_log(values))
        seen = [(f.commit, f.path, f.start_line, f.text.count("\n")) for f in fragments]
        self.assertEqual(seen, [
            (C3, "config/app.env", 2, 1),
            (C3, "ops/team notes.md", 1, 3),
            (C2, "deploy/id_ed25519", 1, 4),
            (C1, "config/app.env", 1, 4),
            (C1, "src/café.py", 1, 2),
        ])
        self.assertEqual(fragments[0].text, "AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}\n")
        self.assertEqual((fragments[0].author, fragments[0].email, fragments[0].date), ("Dana Ops", "dana@example.test", "2026-10-30T17:05:00Z"))
        self.assertEqual(fragments[0].message, "Move the AWS key to the environment\n\nThe webhook stays for now.")
        self.assertEqual(fragments[3].date, "2026-10-29T01:30:00Z")
        self.assertNotIn(values["SLACK_BOT"], "".join(f.text for f in fragments))

    def test_hunks_with_context_lines_split_into_runs_of_added_lines(self):
        patch = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,3 +1,4 @@\n a\n+b\n c\n+d\n"
        fragments = main.parse_patch(patch)
        self.assertEqual([(f.start_line, f.text) for f in fragments], [(2, "b\n"), (4, "d\n")])

    def test_unquote_path_handles_c_escapes_and_the_trailing_tab(self):
        self.assertEqual(main.unquote_path('"b/caf\\303\\251.py"'), "b/café.py")
        self.assertEqual(main.unquote_path('"b/a\\"q\\\\.txt"'), 'b/a"q\\.txt')
        self.assertEqual(main.unquote_path("b/team notes.md\t"), "b/team notes.md")

    def test_parse_messages_reads_nul_separated_records_and_converts_dates_to_utc(self):
        pat = github_pat()
        text = messages_output([
            (C3, "Dana Ops", "dana@example.test", "2026-10-30T18:05:00+01:00", f"debug\n\nused {pat} for now\n"),
            (C2, "Sam Dev", "sam@example.test", "2026-10-29T09:00:00+00:00", "Add the deploy key\n"),
        ])
        fragments = main.parse_messages(text)
        self.assertEqual([(f.commit, f.where, f.date) for f in fragments], [(C3, "message", "2026-10-30T17:05:00Z"), (C2, "message", "2026-10-29T09:00:00Z")])
        findings, _ = main.scan(fragments, main.build_config())
        self.assertEqual([(f.rule, f.where, f.commit, f.line, f.file) for f in findings], [("github-pat", "message", C3, 3, "")])

    def test_git_scan_reports_commit_file_and_line_for_every_planted_secret(self):
        values = fixture_values()
        pat = github_pat()
        git = FakeGit(fixture_log(values), messages_output([(C3, "Dana Ops", "dana@example.test", "2026-10-30T18:05:00+01:00", f"rotate {pat}\n")]))
        findings, _ = main.scan(main.git_fragments("repo", git), main.build_config())
        self.assertEqual(sorted((f.commit, f.file, f.line, f.rule) for f in findings), sorted([
            (C3, "ops/team notes.md", 3, "slack-webhook-url"),
            (C2, "deploy/id_ed25519", 1, "private-key"),
            (C1, "config/app.env", 2, "aws-access-token"),
            (C1, "config/app.env", 3, "aws-secret-access-key"),
            (C1, "src/café.py", 2, "generic-password"),
            (C3, "", 1, "github-pat"),
        ]))
        log_call = " ".join(git.calls[0])
        self.assertIn("log -p -U0 --full-history --all --diff-filter=tuxdb", log_call)
        findings, _ = main.scan(main.git_fragments("repo", git, messages=False), main.build_config())
        self.assertNotIn("message", {f.where for f in findings})

    def test_staged_mode_scans_git_diff_staged_and_has_no_commit(self):
        pat = github_pat()
        git = FakeGit(staged=f"diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+import os\n+GH = '{pat}'\n")
        code, out, _ = run(["staged", "repo", "--json"], git)
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual([(f["rule"], f["file"], f["line"], f["commit"], f["fingerprint"]) for f in report["findings"]], [("github-pat", "new.py", 2, "", "new.py:github-pat:2")])
        self.assertIn("diff -U0 --no-color --no-ext-diff --staged .", " ".join(git.calls[0]))


class BaselineTests(unittest.TestCase):
    def test_a_baseline_from_a_previous_report_hides_known_findings_and_keeps_new_ones(self):
        old, new = github_pat(), github_pat()
        config = main.build_config()
        findings, stats = scan_text(f"A={old}\n")
        report = main.report_json("dir", ".", findings, stats, config)
        baseline = main.Baseline(main.baseline_entry(item) for item in report["findings"])
        findings, stats = scan_text(f"A={old}\nB={new}\n", baseline=baseline)
        self.assertEqual([f.secret for f in findings], [new])
        self.assertEqual(stats.suppressed["baseline"], 1)
        self.assertNotIn(old, json.dumps(report))

    def test_the_baseline_survives_a_moved_line_in_a_working_tree_scan(self):
        token = github_pat()
        findings, _ = scan_text(f"A={token}\n")
        baseline = main.Baseline([main.baseline_entry(main.finding_json(findings[0], main.build_config()))])
        moved, stats = scan_text(f"# new header\n\nA={token}\n", baseline=baseline)
        self.assertEqual((moved, stats.suppressed["baseline"]), ([], 1))

    def test_a_gitleaks_json_report_works_as_a_baseline_even_when_redacted(self):
        kept, redacted, fresh = github_pat(), github_pat(), github_pat()
        gitleaks_report = [
            {"RuleID": "github-pat", "File": "app.env", "Commit": "", "StartLine": 1, "Secret": kept, "Fingerprint": "app.env:github-pat:1"},
            {"RuleID": "github-pat", "File": "app.env", "Commit": "", "StartLine": 2, "Secret": "REDACTED", "Fingerprint": "app.env:github-pat:2"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gitleaks.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(gitleaks_report, handle)
            baseline = main.load_baseline(path)
        findings, stats = scan_text(f"A={kept}\nB={redacted}\nC={fresh}\n", baseline=baseline)
        self.assertEqual([f.secret for f in findings], [fresh])
        self.assertEqual(stats.suppressed["baseline"], 2)


class DirScanTests(unittest.TestCase):
    def test_dir_scan_skips_the_git_store_allowlisted_paths_binaries_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tokens = {name: github_pat() for name in ("git", "testdata", "src", "binary")}
            files = {
                ".git/config": f"url = https://x:{tokens['git']}@example.test\n",
                "testdata/fake.env": f"GH={tokens['testdata']}\n",
                "src/app.env": f"GH={tokens['src']}\n",
                "assets/logo.png": "not really a png\n",
            }
            for rel, text in files.items():
                os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
                with open(os.path.join(tmp, rel), "w", encoding="utf-8") as handle:
                    handle.write(text)
            with open(os.path.join(tmp, "src", "blob.bin"), "wb") as handle:
                handle.write(b"\x00\x01" + tokens["binary"].encode())
            if hasattr(os, "symlink"):
                os.symlink(os.path.join(tmp, "src", "app.env"), os.path.join(tmp, "link.env"))
            config = main.build_config({"allowlists": [{"description": "fixtures", "paths": [r"(?:^|/)testdata/"]}]})
            stats = main.Stats()
            findings, stats = main.scan(main.dir_fragments(tmp, config, stats), config, stats=stats)
            self.assertEqual([(os.path.relpath(f.file, tmp), f.rule) for f in findings], [("src/app.env", "github-pat")])
            self.assertEqual(stats.paths_skipped, 3)
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                findings, _ = main.scan(main.dir_fragments(".", config, main.Stats()), config)
            finally:
                os.chdir(previous)
            self.assertEqual([f.file for f in findings], ["src/app.env"])


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.token = github_pat()
        with open(os.path.join(self.tmp, "app.env"), "w", encoding="utf-8") as handle:
            handle.write(f"REGION=eu-central-1\nGH={self.token}\n")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_the_json_report_never_contains_the_raw_secret(self):
        code, out, _ = run(["dir", self.tmp, "--json"])
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertNotIn(self.token, out)
        finding = report["findings"][0]
        self.assertEqual((finding["rule"], finding["line"], finding["secret"]), ("github-pat", 2, "ghp_****"))
        self.assertEqual(finding["secret_sha256"], hashlib.sha256(self.token.encode()).hexdigest())
        self.assertIn("Revoke", finding["rotate"])
        self.assertEqual(report["remediation"]["order"][0].split()[0], "Rotate")

    def test_the_sarif_report_has_rules_results_and_physical_locations(self):
        code, out, _ = run(["dir", self.tmp, "--sarif"])
        sarif = json.loads(out)
        self.assertEqual((code, sarif["version"]), (1, "2.1.0"))
        run_ = sarif["runs"][0]
        self.assertEqual([r["id"] for r in run_["tool"]["driver"]["rules"]], ["github-pat"])
        result = run_["results"][0]
        region = result["locations"][0]["physicalLocation"]["region"]
        self.assertEqual((result["ruleId"], region["startLine"], region["startColumn"], region["endColumn"]), ("github-pat", 2, 4, 44))
        self.assertTrue(result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"].endswith("app.env"))
        self.assertNotIn(self.token, out)

    def test_filter_repo_expressions_are_written_privately_and_rematch_multiline_secrets(self):
        lines = pem_lines()
        with open(os.path.join(self.tmp, "id_rsa"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        out_path = os.path.join(self.tmp, "..", os.path.basename(self.tmp) + "-expressions.txt")
        try:
            code, _, _ = run(["dir", self.tmp, "--replace-text", out_path])
            self.assertEqual(code, 1)
            self.assertEqual(stat.S_IMODE(os.stat(out_path).st_mode), 0o600)
            with open(out_path, encoding="utf-8") as handle:
                expressions = handle.read().splitlines()
        finally:
            if os.path.exists(out_path):
                os.remove(out_path)
        self.assertIn(f"literal:{self.token}==>REMOVED-github-pat", expressions)
        regex_line = next(e for e in expressions if e.startswith("regex:"))
        pattern, replacement = regex_line[len("regex:"):].rsplit("==>", 1)
        self.assertEqual(replacement, "REMOVED-private-key")
        self.assertIsNotNone(re.search(pattern, "\n".join(lines)))

    def test_the_gitleaks_export_carries_every_rule_keyword_and_allowlist(self):
        config = main.build_config()
        toml = main.gitleaks_toml(config)
        self.assertEqual(toml.count("[[rules]]"), len(config.rules))
        self.assertIn('id = "aws-access-token"', toml)
        self.assertIn("regex = '''" + config.rule("github-pat").regex + "'''", toml)
        self.assertIn('keywords = ["ghp_"]', toml)
        self.assertIn("[[rules.allowlists]]\ndescription = \"AWS documentation example key\"\nregexes = ['''.+EXAMPLE$''']", toml)
        self.assertIn("[[allowlists]]", toml)
        self.assertIn('stopwords = ["example"', toml)
        with self.assertRaises(main.InputError):
            main.toml_literal("a'''b")

    def test_exit_codes_are_zero_clean_one_leak_two_bad_input(self):
        clean = tempfile.mkdtemp()
        try:
            with open(os.path.join(clean, "README.md"), "w", encoding="utf-8") as handle:
                handle.write("nothing secret\n")
            self.assertEqual(run(["dir", clean])[0], 0)
            self.assertEqual(run(["dir", self.tmp])[0], 1)
            code, _, err = run(["dir", self.tmp, "--config", os.path.join(clean, "missing.json")])
            self.assertEqual(code, 2)
            self.assertIn("cannot read config", err)
            bad = os.path.join(clean, "bad.json")
            with open(bad, "w", encoding="utf-8") as handle:
                json.dump({"rules": [{"id": "broken", "regex": "(unclosed"}]}, handle)
            code, _, err = run(["dir", self.tmp, "--config", bad])
            self.assertEqual(code, 2)
            self.assertIn("rule broken: bad regex", err)
            code, _, err = run(["dir", os.path.join(clean, "nope")])
            self.assertEqual(code, 2)

            def broken(args):
                raise main.InputError("git -C nope log ... failed: fatal: not a git repository")

            code, _, err = run(["git", "nope"], broken)
            self.assertEqual(code, 2)
            self.assertIn("not a git repository", err)
        finally:
            shutil.rmtree(clean)

    def test_a_config_file_extends_the_defaults_and_can_disable_rules(self):
        config = main.build_config({"disabled_rules": ["jwt"], "rules": [{"id": "internal-token", "regex": "itk_[a-z0-9]{24}", "keywords": ["itk_"], "entropy": 3.5}]})
        ids = [r.id for r in config.rules]
        self.assertNotIn("jwt", ids)
        self.assertEqual(ids[-1], "internal-token")
        findings, _ = scan_text("T=itk_" + rand(string.ascii_lowercase + string.digits, 24) + "\n", config=config)
        self.assertEqual(rules_of(findings), ["internal-token"])
        with self.assertRaises(main.InputError):
            main.build_config({"rules": [{"id": "x", "regex": "a", "colour": "red"}]})


class HookTests(unittest.TestCase):
    def test_install_hook_writes_an_executable_hook_and_refuses_a_foreign_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            git = FakeGit(hooks=".git/hooks\n")
            path = main.install_hook(tmp, git, python="/usr/bin/python3")
            self.assertEqual(path, os.path.join(tmp, ".git", "hooks", "pre-commit"))
            self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR)
            with open(path, encoding="utf-8") as handle:
                script = handle.read()
            self.assertTrue(script.startswith("#!/bin/sh\n"))
            self.assertIn("staged", script)
            main.install_hook(tmp, git, python="/usr/bin/python3")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\nnpm test\n")
            with self.assertRaises(main.InputError):
                main.install_hook(tmp, git)
            main.install_hook(tmp, git, force=True)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class RealGitTests(unittest.TestCase):
    def git(self, repo, *args):
        subprocess.run(["git", "-C", repo, "-c", "user.name=CC Test", "-c", "user.email=cc@example.test", "-c", "commit.gpgsign=false"] + list(args), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_history_deleted_files_commit_messages_and_staged_changes_in_a_real_repository(self):
        env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        with tempfile.TemporaryDirectory() as repo, mock.patch.dict(os.environ, env):
            self.git(repo, "init", "-q")
            key, pat, staged = aws_key_id(), github_pat(), github_pat()
            os.makedirs(os.path.join(repo, "config"))
            with open(os.path.join(repo, "config", "old.env"), "w", encoding="utf-8") as handle:
                handle.write(f"REGION=eu-central-1\nAWS_ACCESS_KEY_ID={key}\n")
            self.git(repo, "add", "-A")
            self.git(repo, "commit", "-q", "-m", "add config")
            self.git(repo, "rm", "-q", "config/old.env")
            self.git(repo, "commit", "-q", "-m", "remove config")
            with open(os.path.join(repo, "notes.txt"), "w", encoding="utf-8") as handle:
                handle.write("notes\n")
            self.git(repo, "add", "notes.txt")
            self.git(repo, "commit", "-q", "-m", f"debug with {pat}")
            findings, stats = main.scan(main.git_fragments(repo), main.build_config())
            self.assertEqual(sorted((f.where, f.file, f.line, f.rule) for f in findings), [("file", "config/old.env", 2, "aws-access-token"), ("message", "", 1, "github-pat")])
            self.assertTrue(all(len(f.commit) == 40 and f.date.endswith("Z") for f in findings))
            self.assertEqual(stats.paths_skipped, 0)
            with open(os.path.join(repo, "new.py"), "w", encoding="utf-8") as handle:
                handle.write(f"GH = '{staged}'\n")
            self.git(repo, "add", "new.py")
            findings, _ = main.scan(main.staged_fragments(repo), main.build_config())
            self.assertEqual([(f.file, f.line, f.commit, f.secret) for f in findings], [("new.py", 1, "", staged)])


if __name__ == "__main__":
    unittest.main()
