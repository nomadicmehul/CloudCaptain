import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
sys.path.insert(0, CODE)

import main  # noqa: E402

FIXTURES = os.path.join(CODE, "fixtures")
DEPS = os.path.join(FIXTURES, "deps")
AVAILABLE = os.path.join(FIXTURES, "available.json")

PLANTED = {
    (".github/workflows/ci.yml", "actions/checkout", "major", "v5"),
    (".github/workflows/ci.yml", "docker/build-push-action", "major", "v7.0.0"),
    ("requirements.txt", "urllib3", "major", "2.2.3"),
    ("requirements.txt", "gunicorn", "major", "23.0.0"),
    ("web/package.json", "express", "major", "5.1.0"),
    ("web/package.json", "tiny-lib", "major", "0.5.0"),
    ("web/package.json", "chalk", "major", "5.4.1"),
    ("web/package.json", "eslint", "major", "9.17.0"),
    ("web/package.json", "prettier", "major", "3.4.2"),
    (".github/workflows/ci.yml", "actions/setup-python", "minor", "v5.6.0"),
    (".github/workflows/ci.yml", "docker/build-push-action", "minor", "v6.18.0"),
    (".github/workflows/ci.yml", "alpine", "minor", "3.22"),
    ("Dockerfile", "python", "minor", "3.13-slim"),
    ("Dockerfile", "nginx", "minor", "1.29.3-alpine"),
    ("requirements.txt", "requests", "minor", "2.32.3"),
    ("requirements.txt", "flask", "minor", "3.1.1"),
    ("web/package.json", "express", "minor", "4.21.2"),
    ("web/package.json", "zod", "minor", "3.24.1"),
    ("web/package.json", "chalk", "minor", "4.1.2"),
    ("web/package.json", "typescript", "minor", "5.6.3"),
    ("web/package.json", "eslint", "minor", "8.57.0"),
    ("requirements.txt", "urllib3", "patch", "1.26.20"),
    ("requirements.txt", "pyyaml", "patch", "6.0.2"),
    ("web/package.json", "lodash", "patch", "4.17.21"),
    ("web/package.json", "tiny-lib", "patch", "0.4.3"),
    ("Dockerfile", "busybox", "pin", "1.37.0"),
    ("requirements.txt", "structlog", "pin", "25.1.0"),
}


def commit(message, sha="a" * 40):
    return main.make_commit(sha, "Dev", "2026-10-01T10:00:00+00:00", message)


def record(sha, message, date="2026-10-01T23:30:00-02:00"):
    return f"{sha}\x1fDev\x1f{date}\x1f{message}\n\x1e\n"


class FakeGit:
    def __init__(self, tags="", logs=None):
        self.tags = tags
        self.logs = logs or {}
        self.calls = []

    def __call__(self, argv):
        args = list(argv[3:])
        self.calls.append(args)
        if args[0] == "for-each-ref":
            return 0, self.tags, ""
        if args[0] == "log":
            return 0, "".join(self.logs.get(args[-1], [])), ""
        return 0, "", ""


def history_of(tags, unreleased, since_stable=None):
    tag_objs = [main.Tag(name, main.parse_semver(name[1:]), "c" * 40, True) for name in tags]
    releases = [main.Release("", "", "", [commit(m) for m in unreleased])]
    stable = [commit(m) for m in (since_stable if since_stable is not None else unreleased)]
    return main.History(tag_objs, releases, stable)


def run(argv, runner=main.subprocess_runner, today="2026-11-01"):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(argv, runner=runner, today=today)
    return code, out.getvalue(), err.getvalue()


class ConventionalCommitTests(unittest.TestCase):
    def test_header_type_scope_and_bang(self):
        cc, problem = main.parse_message("feat(api)!: rename /items to /inventory\n")
        self.assertEqual(problem, "")
        self.assertEqual((cc.type, cc.scope, cc.description, cc.breaking), ("feat", "api", "rename /items to /inventory", True))
        self.assertEqual(cc.breaking_description, "rename /items to /inventory")
        self.assertEqual(main.parse_message("FIX: handle empty input")[0].type, "fix")

    def test_breaking_change_footer_in_both_spellings_and_only_in_upper_case(self):
        cc, _ = main.parse_message("feat: new auth\n\nWhy we did it.\n\nBREAKING CHANGE: tokens expire after\n  one hour now\nRefs: #12\n")
        self.assertTrue(cc.breaking)
        self.assertEqual(cc.body, "Why we did it.")
        self.assertEqual(cc.breaking_description, "tokens expire after\n  one hour now")
        self.assertEqual([(f.token, f.separator, f.value) for f in cc.footers][1], ("Refs", ": ", "#12"))
        self.assertTrue(main.parse_message("fix: x\n\nBREAKING-CHANGE: y\n")[0].breaking)
        self.assertFalse(main.parse_message("fix: x\n\nbreaking change: y\n")[0].breaking)

    def test_footers_start_at_the_first_trailer_paragraph(self):
        cc, _ = main.parse_message("fix(db): close the pool\n\nFirst paragraph.\n\nSecond paragraph.\n\nCloses #42\nSigned-off-by: Dev <dev@example.invalid>\n")
        self.assertEqual(cc.body, "First paragraph.\n\nSecond paragraph.")
        self.assertEqual([(f.token, f.separator) for f in cc.footers], [("Closes", " #"), ("Signed-off-by", ": ")])
        self.assertFalse(cc.breaking)

    def test_non_conventional_messages_name_the_problem(self):
        cases = {
            "Update README": "no `type: description` header",
            "fix:missing space": "the colon must be followed by a space",
            "feat(): empty scope": "an empty scope",
            "fixup! fix: typo": "an autosquash commit: squash it before the release",
            "Merge branch 'main' into topic": "a merge commit",
            "fix: subject\nbody right after the subject": "the body must start after one blank line",
        }
        for message, problem in cases.items():
            cc, why = main.parse_message(message)
            self.assertIsNone(cc, message)
            self.assertEqual(why, problem, message)

    def test_sections_follow_the_rules_in_order(self):
        cases = {
            "feat: add export": "Added",
            "fix: handle nulls": "Fixed",
            "fix(security): escape names": "Security",
            "feat: deprecate the v1 API": "Deprecated",
            "refactor: Remove the legacy flag": "Removed",
            "feat!: drop Python 3.8": "Removed",
            "perf(db): batch inserts": "Changed",
            "revert: feat: add export": "Changed",
            "docs: remove the old guide": None,
            "chore(deps): bump requests": None,
            "chore!: require Node 22": "Changed",
            "wip: something": None,
            "Update README": None,
        }
        for message, section in cases.items():
            self.assertEqual(commit(message).section, section, message)


class SemVerTests(unittest.TestCase):
    def test_precedence_matches_the_semver_specification(self):
        ordered = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.10.0", "2.0.0"]
        parsed = [main.parse_semver(v) for v in reversed(ordered)]
        self.assertEqual([str(v) for v in sorted(parsed, key=main.SemVer.precedence)], ordered)
        self.assertEqual(main.parse_semver("1.0.0+build.5").precedence(), main.parse_semver("1.0.0+other").precedence())
        self.assertEqual(str(main.parse_semver("1.2.3-rc.1+sha.abc")), "1.2.3-rc.1+sha.abc")

    def test_invalid_versions_are_rejected(self):
        for text in ("01.0.0", "1.0", "1.0.0-01", "v1.0.0", "1.0.0-", "1.0.0+"):
            with self.assertRaises(main.ReleaseError, msg=text):
                main.parse_semver(text)


class BumpTests(unittest.TestCase):
    def level(self, current, *messages, **policy):
        return main.bump_level([commit(m) for m in messages], main.parse_semver(current), main.Policy(**policy))[0]

    def test_rules_after_one_point_zero(self):
        self.assertEqual(self.level("1.4.2", "fix: a"), "patch")
        self.assertEqual(self.level("1.4.2", "fix: a", "perf: b"), "patch")
        self.assertEqual(self.level("1.4.2", "fix: a", "feat: b"), "minor")
        self.assertEqual(self.level("1.4.2", "feat: b", "fix!: c"), "major")
        self.assertEqual(self.level("1.4.2", "fix: c\n\nBREAKING CHANGE: d"), "major")
        self.assertIsNone(self.level("1.4.2", "docs: a", "chore: b", "Update README"))
        self.assertEqual(main.increment(main.parse_semver("1.4.2"), "major"), main.SemVer(2, 0, 0))

    def test_zero_x_rule_and_the_git_cliff_switches(self):
        self.assertEqual(self.level("0.2.0", "feat!: x"), "minor")
        self.assertEqual(self.level("0.2.0", "feat: x"), "patch")
        self.assertEqual(self.level("0.0.3", "feat!: x"), "patch")
        self.assertEqual(self.level("0.2.0", "feat!: x", breaking_always_bump_major=True), "major")
        self.assertEqual(self.level("0.2.0", "feat: x", features_always_bump_minor=True), "minor")
        self.assertEqual(self.level("0.0.3", "feat!: x", features_always_bump_minor=True), "minor")

    def test_initial_version_and_nothing_to_release(self):
        first = main.decide(history_of([], ["feat: start"]), main.Policy())
        self.assertEqual((str(first.version), first.level), ("0.1.0", "initial"))
        self.assertEqual(str(main.decide(history_of([], ["feat: start"]), main.Policy(initial="1.0.0")).version), "1.0.0")
        none = main.decide(history_of(["v1.0.0"], ["docs: typo", "Update README"]), main.Policy())
        self.assertIsNone(none.version)
        self.assertIn("nothing to release", none.reason)

    def test_prerelease_counter_promotion_and_channel_order(self):
        tags = ["v1.4.0", "v1.5.0-rc.1"]
        since_stable = ["feat: export", "fix: crash"]
        rc2 = main.decide(history_of(tags, ["fix: crash"], since_stable), main.Policy(), "rc")
        self.assertEqual(str(rc2.version), "1.5.0-rc.2")
        self.assertEqual(str(main.decide(history_of(tags, [], since_stable), main.Policy()).version), "1.5.0")
        self.assertIsNone(main.decide(history_of(tags, [], since_stable), main.Policy(), "rc").version)
        major = main.decide(history_of(tags, ["feat!: new API"], since_stable + ["feat!: new API"]), main.Policy(), "rc")
        self.assertEqual(str(major.version), "2.0.0-rc.1")
        with self.assertRaisesRegex(main.ReleaseError, "would not be greater than the latest tag v1.5.0-rc.1"):
            main.decide(history_of(tags, ["fix: crash"], since_stable), main.Policy(), "beta")
        with self.assertRaisesRegex(main.ReleaseError, "channel"):
            main.decide(history_of(tags, ["fix: crash"], since_stable), main.Policy(), "rc.1")


class HistoryTests(unittest.TestCase):
    def test_parse_tags_keeps_prefixed_semver_tags_in_precedence_order(self):
        text = "\n".join([
            "refs/tags/v0.10.0 tag 1111111111111111111111111111111111111111 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "refs/tags/v0.9.0 commit bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ",
            "refs/tags/nightly commit cccccccccccccccccccccccccccccccccccccccc",
            "refs/tags/v1.2 commit dddddddddddddddddddddddddddddddddddddddd",
            "refs/tags/app/v2.0.0 commit eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        ])
        tags = main.parse_tags(text, "v")
        self.assertEqual([(t.name, t.annotated, t.commit[0]) for t in tags], [("v0.9.0", False, "b"), ("v0.10.0", True, "a")])
        self.assertEqual([t.name for t in main.parse_tags(text, "app/v")], ["app/v2.0.0"])

    def test_load_history_splits_commits_by_tag_range(self):
        tags = "refs/tags/v0.1.0 tag 1111 " + "a" * 40 + "\nrefs/tags/v0.2.0 commit " + "c" * 40 + "\n"
        fake = FakeGit(tags, {
            "refs/tags/v0.1.0": [record("a" * 40, "feat: first")],
            "refs/tags/v0.1.0..refs/tags/v0.2.0": [record("c" * 40, "fix: second"), record("b" * 40, "docs: notes")],
            "refs/tags/v0.2.0..HEAD": [record("e" * 40, "feat!: third"), record("d" * 40, "Update README")],
        })
        history = main.load_history("/repo", "v", fake)
        self.assertEqual([r.version for r in history.releases], ["", "0.2.0", "0.1.0"])
        self.assertEqual([c.subject for c in history.releases[1].commits], ["docs: notes", "fix: second"])
        self.assertEqual(history.releases[1].date, "2026-10-02")
        self.assertIs(history.since_stable, history.unreleased.commits)
        self.assertEqual(fake.calls[0][:3], ["for-each-ref", "--merged", "HEAD"])
        self.assertEqual(str(main.decide(history, main.Policy()).version), "0.3.0")

    def test_since_stable_spans_prereleases(self):
        tags = "refs/tags/v1.0.0 commit " + "a" * 40 + "\nrefs/tags/v1.1.0-rc.1 commit " + "b" * 40 + "\n"
        fake = FakeGit(tags, {
            "refs/tags/v1.0.0..refs/tags/v1.1.0-rc.1": [record("b" * 40, "feat: export")],
            "refs/tags/v1.1.0-rc.1..HEAD": [record("c" * 40, "fix: crash")],
            "refs/tags/v1.0.0..HEAD": [record("c" * 40, "fix: crash"), record("b" * 40, "feat: export")],
        })
        history = main.load_history("/repo", "v", fake)
        self.assertEqual([c.subject for c in history.since_stable], ["feat: export", "fix: crash"])
        self.assertEqual(str(main.decide(history, main.Policy(), "rc").version), "1.1.0-rc.2")

    def test_a_git_failure_becomes_an_error_with_gits_message(self):
        broken = lambda argv: (128, "", "fatal: not a git repository (or any of the parent directories): .git\n")
        code, out, err = run(["next", "--repo", "/nowhere"], runner=broken)
        self.assertEqual(code, 2)
        self.assertIn("not a git repository", err)


class ChangelogTests(unittest.TestCase):
    def release(self):
        messages = ["fix(security): escape names", "feat(api)!: rename items\n\nBREAKING CHANGE: call /inventory", "docs: typo", "fix: close pool", "feat: deprecate v1 export"]
        return main.Release("0.3.0", "v0.3.0", "2026-11-01", [commit(m) for m in messages])

    def test_entries_are_grouped_in_keep_a_changelog_order(self):
        lines = main.render_release(self.release())
        self.assertEqual(lines[0], "## [0.3.0] - 2026-11-01")
        self.assertEqual([l for l in lines if l.startswith("### ")], ["### Added", "### Deprecated", "### Fixed", "### Security"])
        self.assertIn("- **BREAKING** **api:** rename items", lines)
        self.assertIn("- **security:** escape names", lines)
        self.assertNotIn("typo", "\n".join(lines))

    def test_full_changelog_has_header_unreleased_and_compare_links(self):
        releases = [main.Release("", "", "", []), self.release(), main.Release("0.2.0", "v0.2.0", "2026-10-01", [commit("feat: x")])]
        text = main.render_changelog(releases, "https://github.com/acme/inventory/")
        self.assertTrue(text.startswith("# Changelog\n"))
        self.assertIn("## [Unreleased]\n", text)
        self.assertIn("[Unreleased]: https://github.com/acme/inventory/compare/v0.3.0...HEAD", text)
        self.assertIn("[0.3.0]: https://github.com/acme/inventory/compare/v0.2.0...v0.3.0", text)
        self.assertIn("[0.2.0]: https://github.com/acme/inventory/releases/tag/v0.2.0", text)

    def test_tag_message_keeps_headings_and_lists_breaking_changes(self):
        message = main.tag_message(self.release(), "v0.2.0")
        self.assertTrue(message.startswith("Release v0.3.0\n\n### Added"))
        self.assertTrue(message.endswith("Breaking changes:\n- call /inventory\n"))
        promoted = main.tag_message(main.Release("1.5.0", "v1.5.0", "2026-11-01", []), "v1.5.0-rc.2")
        self.assertIn("Promotes v1.5.0-rc.2 with no further changes.", promoted)

    def test_release_command_tags_with_whitespace_cleanup(self):
        tags = "refs/tags/v1.0.0 commit " + "a" * 40 + "\n"
        fake = FakeGit(tags, {"refs/tags/v1.0.0": [record("a" * 40, "feat: one")], "refs/tags/v1.0.0..HEAD": [record("b" * 40, "fix: two")]})
        code, out, _ = run(["release", "--repo", "/repo", "--sign"], runner=fake)
        self.assertEqual(code, 0)
        tag_call = next(c for c in fake.calls if c[0] == "tag")
        self.assertEqual(tag_call[:3], ["tag", "-s", "--cleanup=whitespace"])
        self.assertEqual(tag_call[-1], "v1.0.1")
        self.assertIn("### Fixed", tag_call[-2])
        self.assertIn("created signed tag v1.0.1", out)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class RealRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo)
        self.git("init", "--quiet")
        for key, value in (("user.name", "Release Bot"), ("user.email", "bot@example.invalid"), ("commit.gpgsign", "false"), ("tag.gpgsign", "false")):
            self.git("config", key, value)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True, text=True).stdout

    def commit(self, message):
        with open(os.path.join(self.repo, "app.txt"), "a", encoding="utf-8") as handle:
            handle.write(message + "\n")
        self.git("add", "app.txt")
        self.git("commit", "--quiet", "-m", message)

    def test_next_changelog_and_an_annotated_tag_on_a_real_repository(self):
        self.commit("feat: first endpoint")
        self.git("tag", "-a", "v1.0.0", "-m", "v1.0.0")
        self.commit("fix(api): handle empty ids")
        self.commit("Update README")
        self.commit("feat(cli): add --json")
        code, out, err = run(["next", "--repo", self.repo])
        self.assertEqual((code, out.strip()), (0, "v1.1.0"), err)
        code, out, _ = run(["changelog", "--repo", self.repo, "--json"])
        sections = json.loads(out)[0]["sections"]
        self.assertEqual(sections, {"Added": ["- **cli:** add --json"], "Fixed": ["- **api:** handle empty ids"]})
        code, out, err = run(["release", "--repo", self.repo, "--changelog", "CHANGELOG.md", "--commit"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.git("cat-file", "-t", "v1.1.0").strip(), "tag")
        self.assertIn("### Added", self.git("for-each-ref", "refs/tags/v1.1.0", "--format=%(contents)"))
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "chore(release): v1.1.0")
        with open(os.path.join(self.repo, "CHANGELOG.md"), encoding="utf-8") as handle:
            self.assertIn("## [1.1.0] - 2026-11-01", handle.read())
        code, _, err = run(["next", "--repo", self.repo])
        self.assertEqual(code, 1)
        self.assertIn("nothing to release", err)


class VersionSchemeTests(unittest.TestCase):
    def test_pep440_ordering(self):
        ordered = ["1.0.dev0", "1.0a1", "1.0b2", "1.0rc1", "1.0", "1.0.post1", "1.1", "2.0", "1!0.1"]
        parsed = [main.pep440(v) for v in reversed(ordered)]
        self.assertEqual([v.text for v in sorted(parsed, key=lambda v: v.key)], ordered)
        self.assertEqual(main.pep440("1.0").key, main.pep440("1.0.0").key)
        self.assertEqual(main.pep440("2.0.0-RC1").key, main.pep440("2.0.0rc1").key)
        self.assertTrue(main.pep440("1.0.dev0").pre and main.pep440("1.0a1").pre and not main.pep440("1.0.post1").pre)

    def test_pip_specifiers(self):
        def ok(version, spec):
            return main.pip_satisfies(main.pep440(version), main.pip_specifiers(spec))
        self.assertTrue(ok("1.26.20", "~=1.26.18"))
        self.assertFalse(ok("2.0.0", "~=1.26.18"))
        self.assertTrue(ok("1.4.9", "==1.4.*"))
        self.assertFalse(ok("1.5.0", "==1.4.*"))
        self.assertFalse(ok("22.0.0", ">=21.2,!=22.0.0,<23"))
        self.assertTrue(ok("22.0.1", ">=21.2,!=22.0.0,<23"))
        self.assertFalse(ok("23.0.0rc1", "<23"))
        self.assertFalse(ok("3.0.0b1", ">=2"))
        self.assertTrue(ok("3.0.0b1", ">=3.0.0a1"))
        self.assertFalse(ok("1.0.post1", ">1.0"))

    def test_npm_ranges_desugar_like_node_semver(self):
        def ok(version, text):
            return main.npm_satisfies(main.parse_semver(version), main.npm_range(text))
        self.assertEqual(main.npm_range("^0.2.3"), [[(">=", main.SemVer(0, 2, 3)), ("<", main.SemVer(0, 3, 0, ("0",)))]])
        self.assertTrue(ok("0.2.9", "^0.2.3"))
        self.assertFalse(ok("0.3.0", "^0.2.3"))
        self.assertFalse(ok("0.0.4", "^0.0.3"))
        self.assertTrue(ok("1.2.9", "~1.2"))
        self.assertFalse(ok("1.3.0", "~1.2.3"))
        self.assertTrue(ok("1.9.0", "1.x"))
        self.assertTrue(ok("2.3.9", "1.2.3 - 2.3"))
        self.assertFalse(ok("2.4.0", "1.2.3 - 2.3"))
        self.assertTrue(ok("3.4.2", "^2.8.8 || ^3.0.0"))
        self.assertTrue(ok("8.57.0", ">= 8.0.0 < 9.0.0"))
        self.assertFalse(ok("5.0.0-beta.3", "^4.18.2"))
        self.assertFalse(ok("1.5.0-beta", ">=1.2.3"))
        self.assertTrue(ok("1.2.4-beta.2", ">=1.2.4-beta.1"))
        self.assertFalse(ok("1.0.0-rc.1", "*"))


class DependencyExtractionTests(unittest.TestCase):
    def test_requirements_txt(self):
        text = 'requests==2.31.0\nflask[async]==3.0.3 ; python_version >= "3.9"  # web\npyyaml==6.0.1 \\\n    --hash=sha256:00\nstructlog\n-r dev.txt\npkg @ https://example.com/pkg.whl\n'
        deps = {d.name: d for d in main.extract_pip("requirements.txt", text)}
        self.assertEqual((deps["flask"].value, deps["flask"].current, deps["flask"].style), ("==3.0.3", "3.0.3", "exact"))
        self.assertEqual((deps["pyyaml"].line, deps["pyyaml"].note), (3, "regenerate the --hash lines (pip-compile --generate-hashes)"))
        self.assertEqual(deps["structlog"].style, "unpinned")
        self.assertEqual(deps["pkg"].skip, "a direct URL reference")
        self.assertNotIn("-r", deps)

    def test_dockerfile(self):
        text = "ARG V=3.12\nFROM --platform=$BUILDPLATFORM golang:1.23-alpine AS build\nFROM build AS test\nFROM python:${V}-slim\nFROM localhost:5000/app:1.4.0@sha256:ab\nFROM scratch\nFROM busybox:latest\n"
        deps = main.extract_dockerfile("Dockerfile", text)
        self.assertEqual([(d.name, d.style) for d in deps], [("golang", "exact"), ("build", "stage"), ("python", "arg"), ("localhost:5000/app", "digest"), ("scratch", "scratch"), ("busybox", "unpinned")])
        self.assertEqual((deps[0].current, deps[3].current, deps[3].digest), ("1.23-alpine", "1.4.0", "sha256:ab"))

    def test_github_actions_uses(self):
        text = "steps:\n  - uses: actions/checkout@v4\n  - uses: 'actions/setup-node@v4.1.0'\n  - uses: github/codeql-action/init@" + "f" * 40 + " # v3.28.0\n  - uses: ./local\n  - uses: org/act@main\n  - uses: docker://alpine:3.21\n  - uses: org/other@" + "e" * 40 + "\n"
        deps = main.extract_actions("ci.yml", text)
        self.assertEqual([(d.name, d.style, d.current) for d in deps], [
            ("actions/checkout", "floating", "v4"),
            ("actions/setup-node", "exact", "v4.1.0"),
            ("github/codeql-action", "digest", "v3.28.0"),
            ("./local", "local", ""),
            ("org/act", "branch", ""),
            ("alpine", "exact", "3.21"),
            ("org/other", "digest", ""),
        ])
        self.assertIn("without a `# vX.Y.Z` comment", deps[-1].skip)


class ProposalTests(unittest.TestCase):
    def setUp(self):
        with open(AVAILABLE, encoding="utf-8") as handle:
            self.available = json.load(handle)

    def test_the_fixture_tree_gets_exactly_the_planted_updates(self):
        deps, updates = main.check_dependencies(DEPS, self.available)
        self.assertEqual({(u.file, u.name, u.group, u.to_version) for u in updates}, PLANTED)
        self.assertEqual(sorted(d.name for d in deps if d.skip), ["./.github/actions/lint", "build", "example-org/deploy-action", "left-pad", "python", "scratch"])
        by = {(u.name, u.to_version): u for u in updates}
        self.assertEqual(by[("docker/build-push-action", "v6.18.0")].new_value, "docker/build-push-action@1234567890abcdef1234567890abcdef12345678 # v6.18.0")
        self.assertEqual(by[("nginx", "1.29.3-alpine")].new_value, "nginx:1.29.3-alpine@sha256:" + "8" * 64)
        self.assertEqual(by[("eslint", "8.57.0")].new_value, ">=8.0.0 <9.0.0")
        self.assertEqual(by[("eslint", "9.17.0")].new_value, "")
        self.assertEqual((by[("zod", "3.24.1")].new_value, by[("zod", "3.24.1")].in_range), ("~3.24.1", False))
        self.assertEqual((by[("express", "4.21.2")].new_value, by[("express", "4.21.2")].in_range), ("^4.21.2", True))
        self.assertEqual(by[("chalk", "5.4.1")].new_value, "5.x")
        self.assertIn("automerge candidate", by[("lodash", "4.17.21")].note)

    def test_docker_tags_keep_their_suffix_and_precision(self):
        dep = main.image_dependency("dockerfile", "Dockerfile", 1, "python:3.12-slim")
        tags = {"docker": {"python": ["3.12.8-slim", "3.13-slim", "3.13-alpine", "3.14-slim-bookworm", "3.15.0a1-slim"]}}
        self.assertEqual([u.to_version for u in main.propose(dep, tags)], ["3.13-slim"])

    def test_zero_x_updates_follow_the_compatibility_line(self):
        self.assertEqual(main.compatibility_line((0, 4, 1)), (0, 4))
        self.assertEqual(main.compatibility_line((1, 2, 3)), (1,))
        self.assertEqual(main.compatibility_line((0, 0, 3)), (0, 0, 3))
        dep = main.Dependency("npm", "package.json", 1, "tiny", "^0.0.3", "0.0.3", "npm", "range")
        updates = main.propose(dep, {"npm": {"tiny": ["0.0.4", "0.1.0"]}})
        self.assertEqual([(u.to_version, u.update_type, u.group) for u in updates], [("0.1.0", "minor", "major")])

    def test_deps_cli_json_and_exit_codes(self):
        code, out, _ = run(["deps", DEPS, "--available", AVAILABLE, "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(len(json.loads(out)["updates"]), len(PLANTED))
        with tempfile.TemporaryDirectory() as tree:
            with open(os.path.join(tree, "requirements.txt"), "w", encoding="utf-8") as handle:
                handle.write("requests==2.32.3\n")
            self.assertEqual(run(["deps", tree, "--available", AVAILABLE])[0], 0)
            bad = os.path.join(tree, "bad.json")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            code, _, err = run(["deps", tree, "--available", bad])
            self.assertEqual(code, 2)
            self.assertIn("is not valid JSON", err)


if __name__ == "__main__":
    unittest.main()
