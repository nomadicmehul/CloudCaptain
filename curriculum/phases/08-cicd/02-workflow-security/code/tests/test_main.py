import contextlib
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
sys.path.insert(0, CODE)

import main  # noqa: E402

FIXTURES = os.path.join(CODE, "fixtures")
VULNERABLE = os.path.join(FIXTURES, "vulnerable")
FIXED = os.path.join(FIXTURES, "fixed")
PIN = "3d3c42e5aac5ba805825da76410c181273ba90b1"


def workflow(text, path="wf.yml"):
    return main.load_workflow(path, textwrap.dedent(text).lstrip("\n"))


def audit(text, visibility="unknown"):
    return main.analyse([workflow(text)], visibility)


def by_rule(findings, rule):
    return [f for f in findings if f.rule == rule]


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class YamlSubsetTests(unittest.TestCase):
    def test_block_scalar_keeps_text_and_records_first_content_line(self):
        doc = main.parse_yaml("jobs:\n  a:\n    steps:\n      - name: x\n        run: |\n          echo one\n\n          echo '#two'\n      - run: echo three # comment\n")
        steps = doc.data["jobs"]["a"]["steps"]
        self.assertEqual(steps[0]["run"], "echo one\n\necho '#two'\n")
        self.assertEqual(doc.value_line("jobs", "a", "steps", 0, "run"), 6)
        self.assertEqual(doc.line("jobs", "a", "steps", 1), 9)
        self.assertEqual(steps[1]["run"], "echo three")

    def test_compact_sequences_and_item_mappings_record_lines(self):
        doc = main.parse_yaml("steps:\n- uses: a/b@v1\n  with:\n    x: 1\n- run: ls\n")
        self.assertEqual(doc.data, {"steps": [{"uses": "a/b@v1", "with": {"x": 1}}, {"run": "ls"}]})
        self.assertEqual(doc.line("steps", 0, "with", "x"), 4)
        self.assertEqual(doc.line("steps", 1, "run"), 5)

    def test_on_stays_a_string_and_comments_respect_quotes(self):
        doc = main.parse_yaml('on: push\nname: Check the PR\'s title # trailing\nmsg: "a #1"\n"quoted key": yes\n')
        self.assertEqual(doc.data, {"on": "push", "name": "Check the PR's title", "msg": "a #1", "quoted key": "yes"})

    def test_folded_and_chomping_variants(self):
        doc = main.parse_yaml("a: >\n  one\n  two\n\n  three\nb: |-\n  kept\n\nc: |+\n  kept\n\nd: x\n")
        self.assertEqual(doc.data["a"], "one two\nthree\n")
        self.assertEqual(doc.data["b"], "kept")
        self.assertEqual(doc.data["c"], "kept\n\n")
        self.assertEqual(doc.data["d"], "x")

    def test_flow_collections(self):
        doc = main.parse_yaml("on: [push, 'pull_request']\nwith: {a: 1, b: [x, y]}\nempty: {}\n")
        self.assertEqual(doc.data, {"on": ["push", "pull_request"], "with": {"a": 1, "b": ["x", "y"]}, "empty": {}})

    def test_errors_name_the_line(self):
        cases = {
            "a:\n\tb: 1\n": (2, "tabs"),
            "a: 1\na: 2\n": (2, "duplicate key"),
            "a: &x 1\n": (1, "anchors"),
            "a: [1, 2\n": (1, "unterminated flow"),
            "a:\n  b: 1\n c: 2\n": (3, "indentation"),
        }
        for source, (line, words) in cases.items():
            with self.subTest(source=source):
                with self.assertRaises(main.YamlError) as caught:
                    main.parse_yaml(source)
                self.assertEqual(caught.exception.line, line)
                self.assertIn(words, str(caught.exception))


class ExpressionTests(unittest.TestCase):
    def test_context_normalisation_handles_brackets_and_filters(self):
        found = [c for c, _ in main.contexts_in(" toJSON(github.event.*.body) || github.event['issue'].title || github.event.commits[0].message ")]
        self.assertEqual(found, ["github.event.*.body", "github.event.issue.title", "github.event.commits.*.message"])

    def test_attacker_contexts_and_safe_ones(self):
        for context in ("github.event.issue.title", "github.head_ref", "github.event.*.body", "github.event.pull_request", "github.event", "github.event.workflow_run.head_branch"):
            self.assertTrue(main.is_attacker_context(context), context)
        for context in ("github.event.issue.number", "github.event.pull_request.head.sha", "github.sha", "github.event_name", "github.event.pull_request.number", "secrets.TOKEN"):
            self.assertFalse(main.is_attacker_context(context), context)

    def test_boolean_functions_make_an_expression_safe(self):
        self.assertEqual(main.attacker_contexts("if [ ${{ contains(github.event.issue.title, '[skip') }} ]"), [])
        self.assertEqual(main.attacker_contexts("${{ startsWith(github.head_ref, 'x') }}"), [])
        self.assertEqual([c for c, _ in main.attacker_contexts("${{ format('{0}', github.head_ref) }}")], ["github.head_ref"])

    def test_tainted_and_secret_environment(self):
        taint = main.tainted_env({"A": "${{ github.event.issue.body }}", "B": "constant"}, {"B": "${{ github.head_ref }}"})
        self.assertEqual(taint, {"A": "github.event.issue.body", "B": "github.head_ref"})
        self.assertEqual(main.secret_env({"T": "${{ secrets.NPM_TOKEN }}", "G": "${{ github.token }}", "X": "y"}), {"T": "secrets.NPM_TOKEN", "G": "github.token"})

    def test_classify_uses(self):
        cases = {
            f"actions/checkout@{PIN}": "sha",
            "actions/checkout@v7": "ref",
            "org/repo/.github/workflows/ci.yml@main": "ref",
            "actions/checkout@3d3c42e": "short-sha",
            "actions/checkout": "no-ref",
            "./.github/actions/build": "local",
            "$/.github/actions/build": "local",
            "docker://alpine:3.22": "docker-tag",
            "docker://alpine": "docker-latest",
            "docker://alpine@sha256:" + "a" * 64: "docker-digest",
        }
        for uses, kind in cases.items():
            self.assertEqual(main.classify_uses(uses), kind, uses)


class RuleTests(unittest.TestCase):
    def test_unpinned_severity_for_first_and_third_party(self):
        findings = by_rule(audit(f"""
            on: push
            permissions: {{}}
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/setup-node@v7
                  - uses: some-org/tool@main
                  - uses: some-org/pinned@{PIN} # v1.0.0
                  - uses: ./local-action
              call:
                uses: other-org/shared/.github/workflows/test.yml@v1
        """), "unpinned-uses")
        self.assertEqual([(f.line, f.severity) for f in findings], [(7, "medium"), (8, "high"), (12, "high")])
        self.assertIn("a tag or branch its owner can move", findings[1].message)

    def test_template_injection_in_run_and_github_script_with_exact_lines(self):
        findings = by_rule(audit("""
            on: pull_request
            permissions: {}
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - run: |
                      echo "safe ${{ github.event.pull_request.number }}"
                      echo "title ${{ github.event.pull_request.title }}"
                  - uses: actions/github-script@v9
                    with:
                      script: |
                        const x = 1;
                        console.log(`${{ github.event.comment.body }}`)
                      github-token: ${{ github.event.pull_request.title }}
        """), "template-injection")
        self.assertEqual([(f.line, f.severity) for f in findings], [(9, "high"), (14, "high")])
        self.assertIn("`github.event.pull_request.title` is pasted into `run`", findings[0].message)

    def test_injection_through_env_expression_but_not_through_shell_variable(self):
        findings = by_rule(audit("""
            on: issue_comment
            permissions: {}
            jobs:
              a:
                runs-on: ubuntu-latest
                env:
                  BODY: ${{ github.event.comment.body }}
                steps:
                  - run: echo "$BODY"
                  - run: echo "${{ env.BODY }}"
        """), "template-injection")
        self.assertEqual([(f.line, f.severity) for f in findings], [(10, "critical")])
        self.assertIn("carries `github.event.comment.body`", findings[0].message)

    def test_untrusted_checkout_is_critical_only_when_code_runs(self):
        base = """
            on: pull_request_target
            permissions: {{}}
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@{pin}
                    with:
                      persist-credentials: false
                      ref: ${{{{ github.event.pull_request.head.sha }}}}
                  {extra}
        """
        quiet = by_rule(audit(base.format(pin=PIN, extra="- uses: actions/upload-artifact@" + PIN + "\n                    with: {name: x, path: dist}")), "untrusted-checkout")
        loud = by_rule(audit(base.format(pin=PIN, extra="- run: make test")), "untrusted-checkout")
        self.assertEqual([(f.line, f.severity) for f in quiet], [(10, "high")])
        self.assertEqual([(f.line, f.severity) for f in loud], [(10, "critical")])
        self.assertIn('step "make test" (line 11) runs it', loud[0].message)
        fetched = by_rule(audit("""
            on: issue_comment
            permissions: {}
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - run: gh pr checkout "$PR"
                  - run: npm test
        """), "untrusted-checkout")
        self.assertEqual([(f.line, f.severity) for f in fetched], [(7, "critical")])

    def test_dangerous_triggers_report_each_trigger_line(self):
        findings = by_rule(audit("""
            on:
              push:
              pull_request_target:
                types: [opened]
              issue_comment:
            permissions: {}
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - run: echo hi
        """), "dangerous-trigger")
        self.assertEqual([f.line for f in findings], [3, 5])

    def test_permissions_missing_write_all_and_unused_scope(self):
        missing = by_rule(audit("""
            on: push
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - run: git push origin HEAD
        """), "excessive-permissions")
        self.assertEqual([(f.line, f.severity) for f in missing], [(3, "medium")])
        self.assertIn("job a needs `contents: write`", missing[0].fix)
        job_level = by_rule(audit("""
            on: push
            permissions: {}
            jobs:
              a:
                runs-on: ubuntu-latest
                permissions:
                  contents: read
                  id-token: write
                  statuses: write
                steps:
                  - uses: actions/checkout@v7
              b:
                runs-on: ubuntu-latest
                permissions: write-all
                steps:
                  - run: gh pr comment 1 --body hi
        """), "excessive-permissions")
        self.assertEqual([(f.line, f.severity) for f in job_level], [(6, "high"), (14, "high")])
        self.assertIn("`id-token: write`, `statuses: write`", job_level[0].message)
        self.assertIn("`pull-requests: write`", job_level[1].fix)
        exact = by_rule(audit("""
            on: push
            permissions:
              contents: write
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - run: gh release create v1
        """), "excessive-permissions")
        self.assertEqual(exact, [])

    def test_permissions_inferred_from_actions_inputs_and_scripts(self):
        job = workflow("""
            on: push
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v7
                  - uses: aws-actions/configure-aws-credentials@v6
                    with:
                      role-to-assume: arn:aws:iam::123456789012:role/deploy
                  - uses: github/codeql-action/upload-sarif@v4
                  - uses: actions/github-script@v9
                    with:
                      script: github.rest.issues.createComment({}); github.rest.pulls.listFiles({})
                  - uses: mystery/action@v1
                  - run: gh api repos
        """).jobs[0]
        needed, unknown = main.needed_permissions(job)
        self.assertEqual(needed, {"contents": "read", "id-token": "write", "security-events": "write", "issues": "write", "pull-requests": "read"})
        self.assertEqual(unknown, ["mystery/action", "gh api"])

    def test_secrets_to_unpinned_actions_printed_and_pasted(self):
        findings = by_rule(audit("""
            on: push
            permissions: {}
            jobs:
              a:
                runs-on: ubuntu-latest
                env:
                  TOKEN: ${{ secrets.TOKEN }}
                steps:
                  - uses: third/party@v1
                    with:
                      key: ${{ secrets.KEY }}
                  - uses: actions/setup-node@v7
                    with:
                      token: ${{ secrets.KEY }}
                  - run: |
                      echo "$TOKEN" | docker login ghcr.io -u me --password-stdin
                      echo "${TOKEN:0:4}"
                      deploy --key "${{ secrets.KEY }}"
                      printf '%s' "$TOKEN" | base64
                      ./use-token.sh
        """), "secret-exposure")
        self.assertEqual([(f.line, f.severity) for f in findings], [(11, "high"), (17, "high"), (18, "medium"), (19, "high")])
        self.assertIn("`secrets.KEY` is handed to `third/party@v1`", findings[0].message)

    def test_tojson_secrets_and_secrets_inherit(self):
        findings = by_rule(audit("""
            on: push
            permissions: {}
            env:
              ALL: ${{ toJSON(secrets) }}
            jobs:
              a:
                uses: org/repo/.github/workflows/x.yml@""" + PIN + """
                secrets: inherit
        """), "secret-exposure")
        self.assertEqual([(f.line, f.severity) for f in findings], [(4, "high"), (8, "medium")])

    def test_persist_credentials_high_when_the_workspace_is_uploaded(self):
        findings = by_rule(audit(f"""
            on: push
            permissions: {{}}
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@{PIN}
                  - uses: actions/upload-artifact@{PIN}
                    with:
                      name: everything
                      path: .
              b:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@{PIN}
                    with:
                      persist-credentials: true
                  - uses: actions/checkout@{PIN}
                    with:
                      persist-credentials: false
        """), "persist-credentials")
        self.assertEqual([(f.line, f.severity) for f in findings], [(7, "high"), (17, "low")])

    def test_self_hosted_runner_depends_on_visibility(self):
        text = """
            on: pull_request
            permissions: {}
            jobs:
              a:
                runs-on:
                  labels: [self-hosted, gpu]
                steps:
                  - run: make
              b:
                runs-on: ubuntu-latest
                steps:
                  - run: make
        """
        self.assertEqual([(f.line, f.severity) for f in by_rule(audit(text, "public"), "self-hosted-runner")], [(5, "critical")])
        self.assertEqual([f.severity for f in by_rule(audit(text, "unknown"), "self-hosted-runner")], ["low"])
        self.assertEqual(by_rule(audit(text, "private"), "self-hosted-runner"), [])

    def test_github_env_tainted_values_and_computed_values_in_privileged_runs(self):
        findings = by_rule(audit("""
            on: workflow_run
            permissions: {}
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - env:
                      MSG: ${{ github.event.workflow_run.head_commit.message }}
                    run: |
                      echo "MSG=$MSG" >> "$GITHUB_ENV"
                      echo "DIR=$(cat out/dir)" >> $GITHUB_PATH
                      echo "MODE=release" >> "$GITHUB_ENV"
                      printf '%s\\n' "$MSG" | tee -a "$GITHUB_ENV"
        """), "github-env-injection")
        self.assertEqual([(f.line, f.severity) for f in findings], [(10, "critical"), (11, "medium"), (13, "critical")])
        self.assertIn("GITHUB_PATH", findings[1].message)

    def test_cache_poisoning_source_and_sink_across_workflows(self):
        source = workflow("""
            on: pull_request_target
            permissions: {}
            jobs:
              build:
                runs-on: ubuntu-latest
                cache-mode: write
                steps:
                  - uses: actions/checkout@""" + PIN + """
                    with:
                      persist-credentials: false
                      ref: ${{ github.event.pull_request.head.sha }}
                  - run: npm ci
        """, "untrusted.yml")
        sink = workflow("""
            on:
              release:
                types: [published]
            permissions: {}
            jobs:
              publish:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/setup-go@""" + PIN + """
                  - uses: actions/setup-python@""" + PIN + """
                    with:
                      python-version: "3.12"
        """, "release.yml")
        alone = by_rule(main.analyse([sink]), "cache-poisoning")
        together = by_rule(main.analyse([source, sink]), "cache-poisoning")
        self.assertEqual([(f.file, f.line, f.severity) for f in alone], [("release.yml", 9, "high")])
        self.assertEqual([(f.file, f.line, f.severity) for f in together], [("release.yml", 9, "critical"), ("untrusted.yml", 6, "critical")])
        self.assertIn("untrusted.yml job build (line 6)", together[0].message)

    def test_cache_mode_none_and_implicit_read_default_are_quiet(self):
        findings = audit("""
            on:
              push:
                tags: ["v*"]
            cache-mode: none
            permissions: {}
            jobs:
              publish:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/cache@""" + PIN + """
                    with: {path: x, key: y}
        """) + audit("""
            on: pull_request_target
            permissions: {}
            jobs:
              label:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/cache@""" + PIN + """
                    with: {path: x, key: y}
        """)
        self.assertEqual(by_rule(findings, "cache-poisoning"), [])


class FixtureTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIXTURES, "expected.json"), encoding="utf-8") as handle:
            self.expected = json.load(handle)

    def test_vulnerable_fixtures_match_the_planted_issues_exactly(self):
        findings = main.analyse(main.load_paths([VULNERABLE]), self.expected["visibility"])
        got = sorted((os.path.basename(f.file), f.line, f.rule) for f in findings)
        want = sorted((p["file"], p["line"], p["rule"]) for p in self.expected["planted"])
        self.assertEqual(got, want)

    def test_fixed_fixtures_are_clean_even_on_a_public_repository(self):
        workflows = main.load_paths([FIXED])
        self.assertEqual(sorted(os.path.basename(w.path) for w in workflows), sorted({p["file"] for p in self.expected["planted"]}))
        self.assertEqual(main.analyse(workflows, "public"), [])

    def test_every_rule_is_planted_at_least_once(self):
        self.assertEqual({p["rule"] for p in self.expected["planted"]}, set(main.RULES))

    def test_fixed_pins_are_full_shas_with_a_version_comment(self):
        for name in sorted(os.listdir(FIXED)):
            with open(os.path.join(FIXED, name), encoding="utf-8") as handle:
                for line in handle:
                    if "uses:" in line:
                        self.assertRegex(line, r"uses: [\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$", f"{name}: {line.strip()}")


class OutputTests(unittest.TestCase):
    def test_sarif_document_structure(self):
        findings = main.analyse(main.load_paths([VULNERABLE]), "public")
        document = main.sarif(findings)
        self.assertEqual(document["version"], "2.1.0")
        run = document["runs"][0]
        rule_ids = [rule["id"] for rule in run["tool"]["driver"]["rules"]]
        self.assertEqual(rule_ids, list(main.RULES))
        self.assertEqual(len(run["results"]), len(findings))
        first = run["results"][0]
        self.assertEqual(rule_ids[first["ruleIndex"]], first["ruleId"])
        self.assertIn(first["level"], ("error", "warning", "note"))
        region = first["locations"][0]["physicalLocation"]["region"]
        self.assertGreaterEqual(region["startLine"], 1)
        levels = {r["ruleId"]: r["properties"]["security-severity"] for r in run["results"]}
        self.assertEqual(levels["untrusted-checkout"], "9.5")

    def test_json_report_text_and_exit_codes(self):
        code, out, _ = run_cli("--json", "--public", VULNERABLE)
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual(data["summary"]["findings"], 33)
        self.assertEqual(data["summary"]["by_severity"]["critical"], 11)
        self.assertEqual(data["permissions"][os.path.normpath(os.path.join(VULNERABLE, "pr-preview.yml"))]["comment"]["permissions"], {"pull-requests": "write"})
        code, out, _ = run_cli(FIXED, "--permissions")
        self.assertEqual(code, 0)
        self.assertIn("No findings.", out)
        self.assertIn("issue-triage.yml triage: issues: write", out)
        code, out, _ = run_cli("--min-severity", "critical", "--public", VULNERABLE)
        self.assertEqual(code, 1)
        self.assertIn("11 finding(s): 11 critical", out)

    def test_cli_errors_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.yml")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("on: push\njobs:\n  a:\n\truns-on: x\n")
            notwf = os.path.join(tmp, "notwf.yml")
            with open(notwf, "w", encoding="utf-8") as handle:
                handle.write("name: only a name\n")
            empty = os.path.join(tmp, "empty")
            os.mkdir(empty)
            for path, words in ((bad, "bad.yml:4: tabs"), (notwf, "missing on:"), (empty, "no workflow files"), (os.path.join(tmp, "nope"), "no such file")):
                code, _, err = run_cli(path)
                self.assertEqual(code, 2, path)
                self.assertIn(words, err)

    def test_lockfile_locks_listed_actions_but_not_reusable_workflows(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflows = os.path.join(tmp, ".github", "workflows")
            os.makedirs(workflows)
            with open(os.path.join(workflows, "ci.yml"), "w", encoding="utf-8") as handle:
                handle.write(textwrap.dedent("""\
                    on: push
                    permissions: {}
                    jobs:
                      a:
                        runs-on: ubuntu-latest
                        steps:
                          - uses: octo-org/deploy-action@v3.1.0
                          - uses: octo-org/other-action@v2
                      b:
                        uses: octo-org/shared/.github/workflows/test.yml@v1
                """))
            with open(os.path.join(workflows, "actions.lock"), "w", encoding="utf-8") as handle:
                handle.write(textwrap.dedent("""\
                    version: 'v0.0.3'
                    workflows:
                        '.github/workflows/ci.yml':
                            - 'octo-org/deploy-action@v3.1.0'
                    dependencies:
                        'octo-org/deploy-action@v3.1.0':
                            ref: 'v3.1.0'
                            commit: '""" + PIN + """'
                            owner_id: 44036562
                            repo_id: 197814629
                """))
            loaded = main.load_paths([tmp])
            self.assertEqual(len(loaded), 1)
            unpinned = by_rule(main.analyse(loaded), "unpinned-uses")
            self.assertEqual([(f.line, f.message.split("`")[1]) for f in unpinned], [(8, "octo-org/other-action@v2"), (10, "octo-org/shared/.github/workflows/test.yml@v1")])

    def test_parallel_steps_are_audited_like_serial_ones(self):
        found = audit("""
            on: issues
            permissions: {}
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - parallel:
                      - name: one
                        run: echo "${{ github.event.issue.title }}"
                      - uses: third/party@v1
        """)
        self.assertEqual(sorted((f.rule, f.line) for f in found), [("template-injection", 9), ("unpinned-uses", 10)])

    def test_collect_prefers_the_github_workflows_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, ".github", "workflows"))
            os.makedirs(os.path.join(tmp, "docs"))
            for name in (".github/workflows/ci.yml", ".github/workflows/release.yaml", "docs/example.yml"):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as handle:
                    handle.write("on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps: []\n")
            self.assertEqual([os.path.relpath(p, tmp) for p in main.collect([tmp])], [os.path.join(".github", "workflows", "ci.yml"), os.path.join(".github", "workflows", "release.yaml")])


if __name__ == "__main__":
    unittest.main()
