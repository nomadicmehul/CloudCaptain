import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

CODE = Path(__file__).resolve().parent.parent
SAMPLE = CODE / "sample"
INDEX_SPEC = CODE / "fixtures" / "index.json"
EPOCH = 1761991200


def fake_git(responses):
    calls = []

    def runner(args, cwd):
        calls.append(list(args))
        for prefix, result in responses:
            if list(args[:len(prefix)]) == prefix:
                return result
        return main.CommandResult(128, "", "fatal: not a git repository")

    runner.calls = calls
    return runner


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project_dir = self.tmp / "sample"
        shutil.copytree(SAMPLE, self.project_dir)

    def static_project(self):
        text = (self.project_dir / "pyproject.toml").read_text()
        text = text.replace('dynamic = ["version"]', 'version = "1.2.0"').replace('version-source = "git"', 'module = "cc_envcheck"')
        (self.project_dir / "pyproject.toml").write_text(text)
        return main.load_project(self.project_dir)

    def index(self):
        out = self.tmp / "index"
        main.build_index(INDEX_SPEC, out, EPOCH)
        return out


class TomlTests(unittest.TestCase):
    def test_sample_pyproject_parses_into_tables_arrays_and_inline_tables(self):
        data = main.parse_toml((SAMPLE / "pyproject.toml").read_text())
        self.assertEqual(data["build-system"]["build-backend"], "flit_core.buildapi")
        self.assertEqual(data["project"]["authors"], [{"name": "CloudCaptain", "email": "lessons@cloudcaptain.io"}])
        self.assertEqual(data["project"]["scripts"], {"cc-envcheck": "cc_envcheck.cli:main"})
        self.assertEqual(data["project"]["classifiers"][0], "Environment :: Console")
        self.assertEqual(data["tool"]["shipit"], {"version-source": "git"})

    def test_strings_comments_dotted_keys_and_arrays_of_tables(self):
        text = '# top\na.b = "x # not a comment"  # comment\nc = \'C:\\path\'\nn = 1_000\nt = true\n[[item]]\nk = "\\u00e9"\n[[item]]\nk = "\\"q\\""\n'
        data = main.parse_toml(text)
        self.assertEqual(data["a"], {"b": "x # not a comment"})
        self.assertEqual(data["c"], "C:\\path")
        self.assertEqual((data["n"], data["t"]), (1000, True))
        self.assertEqual(data["item"], [{"k": "\u00e9"}, {"k": '"q"'}])

    def test_errors_name_the_line(self):
        cases = {
            'name = "x"\nname = "y"\n': ":2: duplicate key name",
            'a = "x" b\n': ":1: unexpected text after value",
            '[project]\nreadme = """\nx\n"""\n': ":2: multi-line strings are not supported",
            'x = [1, 2\ny = 3\n': ":2: expected ',' or ']'",
            'x = 1.5\n': ":1: unsupported value",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(main.ConfigError) as caught:
                    main.parse_toml(text, "p.toml")
                self.assertIn(expected, str(caught.exception))


class VersionTests(unittest.TestCase):
    def test_normalisation_follows_pep_440(self):
        cases = {"v1.2.0": "1.2.0", "1.0-RC1": "1.0rc1", "1.0.alpha2": "1.0a2", "1.0-1": "1.0.post1", "2.0.0-dev3": "2.0.0.dev3", "1.0+Local_Build": "1.0+local.build", "1!2.0": "1!2.0"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(main.normalize_version(raw), expected)
        with self.assertRaises(main.ShipitError):
            main.parse_version("1.0-final-final")

    def test_ordering_puts_dev_before_pre_before_final_before_post(self):
        ordered = ["1.0.dev1", "1.0a1", "1.0b2", "1.0rc1", "1.0", "1.0.post1", "1.0.1.dev1+gabc", "1.0.1", "1!0.1"]
        shuffled = list(reversed(ordered))
        self.assertEqual([str(v) for v in sorted(map(main.parse_version, shuffled), key=lambda v: v.key)], ordered)
        self.assertEqual(main.parse_version("1.0").key, main.parse_version("1.0.0").key)

    def test_specifiers_compatible_release_wildcards_and_prereleases(self):
        requirement = main.parse_requirement("cc-term~=0.3.0")
        self.assertEqual([requirement.allows(main.parse_version(v)) for v in ("0.2.0", "0.3.0", "0.3.9", "0.4.0")], [False, True, True, False])
        wildcard = main.parse_requirement("x==1.4.*")
        self.assertEqual([wildcard.allows(main.parse_version(v)) for v in ("1.4", "1.4.7", "1.5.0")], [True, True, False])
        ranged = main.parse_requirement("x>=1.1,<2,!=1.3.0")
        self.assertEqual([ranged.allows(main.parse_version(v)) for v in ("1.0", "1.1", "1.3.0", "1.2.0rc1", "2.0")], [False, True, False, False, False])
        self.assertTrue(main.parse_requirement("x>=1.2.0rc1").allows(main.parse_version("1.2.0rc1")))

    def test_requirements_with_markers_or_extras_are_refused_by_name(self):
        for text in ('x>=1; python_version < "3.10"', "x[socks]>=1"):
            with self.subTest(text=text):
                with self.assertRaises(main.ShipitError) as caught:
                    main.parse_requirement(text)
                self.assertIn("not supported", str(caught.exception))


class GitVersionTests(TempDirTest):
    def test_exact_tag_gives_the_tag_version(self):
        runner = fake_git([(["git", "describe"], main.CommandResult(0, "v1.2.0-0-gabc1234\n"))])
        version = main.version_from_git(self.tmp, runner)
        self.assertEqual((version.version, version.tag, version.distance, version.dirty), ("1.2.0", "v1.2.0", 0, False))
        self.assertIn("--match", runner.calls[0])

    def test_commits_after_a_tag_give_the_next_dev_version(self):
        cases = {
            "v1.2.0-3-gabc1234": "1.2.1.dev3+gabc1234",
            "v1.2.0-0-gabc1234-dirty": "1.2.1.dev0+gabc1234.dirty",
            "v2.0.0rc1-2-gdef5678": "2.0.0rc2.dev2+gdef5678",
            "v1.9-1-g1111111": "1.10.dev1+g1111111",
        }
        for described, expected in cases.items():
            with self.subTest(described=described):
                runner = fake_git([(["git", "describe"], main.CommandResult(0, described + "\n"))])
                self.assertEqual(main.version_from_git(self.tmp, runner).version, expected)

    def test_no_tag_and_no_repository(self):
        runner = fake_git([
            (["git", "describe"], main.CommandResult(128, "", "fatal: No names found")),
            (["git", "rev-list"], main.CommandResult(0, "7\n")),
            (["git", "rev-parse"], main.CommandResult(0, "abc1234\n")),
            (["git", "status"], main.CommandResult(0, "")),
        ])
        self.assertEqual(main.version_from_git(self.tmp, runner).version, "0.1.dev7+gabc1234")
        with self.assertRaises(main.ConfigError) as caught:
            main.version_from_git(self.tmp, fake_git([]))
        self.assertIn("cannot derive a version from git", str(caught.exception))

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_real_repository_tag_then_commit(self):
        env = dict(os.environ, GIT_AUTHOR_DATE="2026-11-01T10:00:00+00:00", GIT_COMMITTER_DATE="2026-11-01T10:00:00+00:00", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(self.tmp / "gitconfig"))
        git = ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "init"], ["tag", "-a", "v1.2.0", "-m", "v1.2.0"]):
            subprocess.run(git + args, cwd=self.project_dir, env=env, check=True, capture_output=True)
        project = main.load_project(self.project_dir)
        self.assertEqual(main.resolve_version(project), "1.2.0")
        (self.project_dir / "README.md").write_text("changed\n")
        subprocess.run(git + ["commit", "-q", "-am", "docs"], cwd=self.project_dir, env=env, check=True, capture_output=True)
        sha = subprocess.run(["git", "rev-parse", "--short=7", "HEAD"], cwd=self.project_dir, capture_output=True, text=True).stdout.strip()
        self.assertEqual(main.resolve_version(project), f"1.2.1.dev1+g{sha}")
        self.assertEqual(main.source_date_epoch({}, self.project_dir), 1793527200)


class ProjectTests(TempDirTest):
    def test_load_project_reads_pep_621_fields(self):
        project = main.load_project(self.project_dir)
        self.assertEqual((project.name, project.version, project.version_source, project.module), ("cc-envcheck", None, "git", "cc_envcheck"))
        self.assertEqual(project.dependencies, ["cc-colors>=1.1,<2"])
        self.assertEqual(project.package_root, self.project_dir / "src")
        self.assertEqual(project.readme_type, "text/markdown")

    def test_configuration_errors_say_what_to_change(self):
        cases = {
            ('dynamic = ["version"]', ""): 'set project.version or list "version" in project.dynamic',
            ('license = "MIT"', 'license = { text = "MIT" }'): "SPDX expression",
            ('name = "cc-envcheck"', 'name = "-bad"'): "valid distribution name",
            ('readme = "README.md"', 'readme = "MISSING.md"'): "MISSING.md, which does not exist",
            ('name = "cc-envcheck"', 'name = "other-tool"'): "cannot find the import package 'other_tool'",
        }
        original = (self.project_dir / "pyproject.toml").read_text()
        for (old, new), expected in cases.items():
            with self.subTest(expected=expected):
                (self.project_dir / "pyproject.toml").write_text(original.replace(old, new))
                with self.assertRaises(main.ConfigError) as caught:
                    main.load_project(self.project_dir)
                self.assertIn(expected, str(caught.exception))

    def test_module_version_source_reads_dunder_version(self):
        text = (self.project_dir / "pyproject.toml").read_text().replace('version-source = "git"', 'module = "cc_envcheck"')
        (self.project_dir / "pyproject.toml").write_text(text)
        project = main.load_project(self.project_dir)
        self.assertEqual((project.version_source, main.resolve_version(project, fake_git([]))), ("module", "1.2.0"))


class WheelTests(TempDirTest):
    def test_names_and_record_hash_format(self):
        self.assertEqual(main.wheel_filename("CC.Env_Check", "1.0rc1"), "cc_env_check-1.0rc1-py3-none-any.whl")
        self.assertEqual(main.dist_info_dir("cc-envcheck", "1.2.0"), "cc_envcheck-1.2.0.dist-info")
        self.assertEqual(main.parse_wheel_filename("cc_envcheck-1.2.0-1-py3-none-any.whl"), ("cc_envcheck", "1.2.0", "py3-none-any"))
        digest = base64.urlsafe_b64encode(hashlib.sha256(b"hello\n").digest()).rstrip(b"=").decode()
        self.assertEqual(main.record_hash(b"hello\n"), "sha256=" + digest)
        self.assertNotIn("=", main.record_hash(b"x")[7:])
        self.assertNotIn("+", main.record_hash(bytes(range(256)) * 3))

    def test_build_writes_metadata_wheel_entry_points_and_record_last(self):
        project = self.static_project()
        result = main.build_wheel(project, "1.2.0", self.tmp / "dist", EPOCH)
        self.assertEqual(result.path.name, "cc_envcheck-1.2.0-py3-none-any.whl")
        info = main.read_wheel(result.path)
        self.assertEqual(info.problems, [])
        self.assertEqual(info.files[-1], "cc_envcheck-1.2.0.dist-info/RECORD")
        self.assertEqual(info.files[:2], ["cc_envcheck/__init__.py", "cc_envcheck/cli.py"])
        self.assertEqual(info.scripts, {"cc-envcheck": "cc_envcheck.cli:main"})
        self.assertEqual(info.metadata["Metadata-Version"], ["2.5"])
        self.assertEqual(info.metadata["Requires-Dist"], ["cc-colors>=1.1,<2"])
        self.assertEqual(info.metadata["Author-email"], ["CloudCaptain <lessons@cloudcaptain.io>"])
        self.assertEqual(info.metadata["License-Expression"], ["MIT"])
        self.assertEqual(info.metadata["Import-Name"], ["cc_envcheck"])
        self.assertTrue(info.metadata["Description"][0].startswith("# cc-envcheck"))
        with zipfile.ZipFile(result.path) as archive:
            record = list(csv.reader(io.StringIO(archive.read(info.dist_info + "/RECORD").decode())))
            self.assertEqual(record[-1], ["cc_envcheck-1.2.0.dist-info/RECORD", "", ""])
            self.assertEqual({entry.date_time for entry in archive.infolist()}, {(2025, 11, 1, 10, 0, 0)})
            self.assertEqual({entry.external_attr >> 16 for entry in archive.infolist()}, {0o100644})

    def test_builds_are_byte_identical_and_epoch_changes_them(self):
        project = self.static_project()
        (self.project_dir / "src" / "cc_envcheck" / "__pycache__").mkdir()
        (self.project_dir / "src" / "cc_envcheck" / "__pycache__" / "cli.cpython-39.pyc").write_bytes(b"junk")
        first = main.build_wheel(project, "1.2.0", self.tmp / "a", EPOCH)
        os.utime(self.project_dir / "src" / "cc_envcheck" / "cli.py", (0, 0))
        second = main.build_wheel(project, "1.2.0", self.tmp / "b", EPOCH)
        third = main.build_wheel(project, "1.2.0", self.tmp / "c", EPOCH + 86400)
        self.assertEqual(first.path.read_bytes(), second.path.read_bytes())
        self.assertNotEqual(first.sha256, third.sha256)
        self.assertNotIn("cc_envcheck/__pycache__/cli.cpython-39.pyc", first.files)
        self.assertEqual(main.build_wheel(project, "1.2.0", self.tmp / "dry", EPOCH, dry_run=True).sha256, first.sha256)
        self.assertFalse((self.tmp / "dry").exists())

    def test_source_date_epoch_from_env_git_or_1980(self):
        self.assertEqual(main.source_date_epoch({"SOURCE_DATE_EPOCH": "1700000000"}, self.tmp, fake_git([])), 1700000000)
        self.assertEqual(main.source_date_epoch({}, self.tmp, fake_git([(["git", "log"], main.CommandResult(0, "1690000000\n"))])), 1690000000)
        self.assertEqual(main.source_date_epoch({}, self.tmp, fake_git([])), 315532800)
        with self.assertRaises(main.ConfigError):
            main.source_date_epoch({"SOURCE_DATE_EPOCH": "yesterday"}, self.tmp, fake_git([]))

    def test_read_wheel_catches_edited_files_unlisted_files_and_unsafe_paths(self):
        project = self.static_project()
        good = main.build_wheel(project, "1.2.0", self.tmp / "dist", EPOCH).path
        bad = self.tmp / "bad" / good.name
        bad.parent.mkdir()
        with zipfile.ZipFile(good) as source, zipfile.ZipFile(bad, "w") as target:
            for entry in source.infolist():
                data = source.read(entry)
                if entry.filename == "cc_envcheck/cli.py":
                    data += b"\nimport os; os.system('id')\n"
                target.writestr(entry, data)
            target.writestr("../escape.py", b"x")
        problems = main.read_wheel(bad).problems
        self.assertTrue(any("cc_envcheck/cli.py: hash" in p for p in problems), problems)
        self.assertTrue(any("unsafe path ../escape.py" in p for p in problems), problems)
        self.assertTrue(any("../escape.py is not listed in RECORD" in p for p in problems), problems)


class LockTests(TempDirTest):
    def test_resolve_picks_highest_allowed_skips_prereleases_and_follows_dependencies(self):
        index = main.scan_index(self.index())
        pins = main.resolve(["cc-colors>=1.1,<2"], index, "cc-envcheck")
        self.assertEqual([(p.name, p.version, p.via) for p in pins], [("cc-colors", "1.1.0", ["cc-envcheck"]), ("cc-term", "0.3.1", ["cc-colors"])])
        for pin in pins:
            self.assertEqual(pin.sha256, hashlib.sha256(pin.path.read_bytes()).hexdigest())

    def test_conflicts_and_missing_versions_are_explained(self):
        index = main.scan_index(self.index())
        with self.assertRaises(main.ShipitError) as caught:
            main.resolve(["cc-colors>=1.1,<2", "cc-term>=0.4"], index, "app")
        self.assertIn("conflict on cc-term", str(caught.exception))
        self.assertIn("app wants cc-term>=0.4", str(caught.exception))
        with self.assertRaises(main.ShipitError) as caught:
            main.resolve(["cc-colors>=3"], index, "app")
        self.assertIn("available: 1.0.0, 1.1.0, 1.2.0rc1, 2.0.0", str(caught.exception))

    def test_lock_text_round_trips_and_every_pin_needs_a_hash(self):
        index = main.scan_index(self.index())
        pins = main.resolve(["cc-colors>=1.1,<2"], index, "cc-envcheck")
        text = main.lock_text(pins, "cc-envcheck", "1.2.0")
        entries = main.parse_lock(text)
        self.assertEqual([(e.name, e.version, e.hashes) for e in entries], [(p.name, p.version, [p.sha256]) for p in pins])
        self.assertIn("cc-colors==1.1.0 \\\n    --hash=sha256:", text)
        with self.assertRaises(main.ShipitError) as caught:
            main.parse_lock("cc-term==0.3.1\n")
        self.assertIn("needs one", str(caught.exception))
        with self.assertRaises(main.ShipitError):
            main.parse_lock("cc-term>=0.3 --hash=sha256:abc\n")


class InstallTests(TempDirTest):
    def locked(self):
        index = self.index()
        pins = main.resolve(["cc-colors>=1.1,<2"], main.scan_index(index), "cc-envcheck")
        lock = self.tmp / "shipit.lock"
        lock.write_text(main.lock_text(pins, "cc-envcheck", "1.2.0"))
        return index, lock

    def test_hash_mismatch_refuses_before_writing_anything(self):
        index, lock = self.locked()
        spec = json.loads(INDEX_SPEC.read_text())
        for package in spec["packages"]:
            if package["version"] == "0.3.1":
                package["files"]["cc_term/__init__.py"] += "print('evil')\n"
        evil_spec = self.tmp / "evil.json"
        evil_spec.write_text(json.dumps(spec))
        evil = self.tmp / "evil"
        main.build_index(evil_spec, evil, EPOCH)
        self.assertEqual(main.read_wheel(evil / "cc_term-0.3.1-py3-none-any.whl").problems, [])
        target = self.tmp / "target"
        with self.assertRaises(main.ShipitError) as caught:
            main.install_locked(lock, evil, target)
        self.assertIn("cc-term==0.3.1: hash mismatch", str(caught.exception))
        self.assertFalse(target.exists())

    def test_install_is_idempotent_and_upgrades_remove_old_files(self):
        index, lock = self.locked()
        target = self.tmp / "target"
        self.assertEqual([r.action for r in main.install_locked(lock, index, target, dry_run=True)], ["would be installed", "would be installed"])
        self.assertFalse(target.exists())
        self.assertEqual([(r.name, r.action) for r in main.install_locked(lock, index, target)], [("cc-colors", "installed"), ("cc-term", "installed")])
        self.assertEqual([r.action for r in main.install_locked(lock, index, target)], ["unchanged", "unchanged"])
        self.assertEqual((target / "cc_term-0.3.1.dist-info" / "INSTALLER").read_text(), "shipit\n")
        result = main.install_wheel(index / "cc_colors-2.0.0-py3-none-any.whl", target)
        self.assertEqual(result.action, "upgraded")
        self.assertFalse((target / "cc_colors-1.1.0.dist-info").exists())
        self.assertTrue((target / "cc_colors-2.0.0.dist-info" / "RECORD").is_file())

    def test_console_script_runs_from_the_target_directory(self):
        index, lock = self.locked()
        target = self.tmp / "target"
        main.install_locked(lock, index, target)
        wheel = main.build_wheel(self.static_project(), "1.2.0", self.tmp / "dist", EPOCH).path
        main.install_wheel(wheel, target)
        script = target / "bin" / "cc-envcheck"
        self.assertTrue(os.access(script, os.X_OK))
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["CC_TEST_SET"] = "1"
        done = subprocess.run([sys.executable, str(script), "--json", "CC_TEST_SET", "CC_TEST_UNSET"], capture_output=True, text=True, env=env, cwd=str(self.tmp))
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"checked": ["CC_TEST_SET", "CC_TEST_UNSET"], "missing": ["CC_TEST_UNSET"]})


class ZipappTests(TempDirTest):
    def test_zipapp_bundles_locked_dependencies_and_runs_with_a_bare_python(self):
        index = self.index()
        pins = main.resolve(["cc-colors>=1.1,<2"], main.scan_index(index), "cc-envcheck")
        project = self.static_project()
        out = self.tmp / "cc-envcheck.pyz"
        result = main.build_zipapp(project, "1.2.0", out, EPOCH, [p.path for p in pins])
        again = main.build_zipapp(project, "1.2.0", self.tmp / "again.pyz", EPOCH, [p.path for p in pins])
        self.assertEqual(result.sha256, again.sha256)
        self.assertTrue(out.read_bytes().startswith(b"#!/usr/bin/env python3\n"))
        self.assertIn("cc_term/__init__.py", result.files)
        self.assertIn("__main__.py", result.files)
        done = subprocess.run([sys.executable, "-I", str(out), "--version"], capture_output=True, text=True, cwd=str(self.tmp))
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "cc-envcheck 1.2.0"), done.stderr)

    def test_zipapp_needs_a_known_script(self):
        project = self.static_project()
        with self.assertRaises(main.ConfigError) as caught:
            main.build_zipapp(project, "1.2.0", self.tmp / "x.pyz", EPOCH, [], script="nope")
        self.assertIn("no console script named 'nope'", str(caught.exception))


class CliTests(TempDirTest):
    def test_build_json_and_quiet(self):
        self.static_project()
        code, out, err = run_cli(["build", "--project", str(self.project_dir), "--out", str(self.tmp / "dist"), "--json"])
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual((data["name"], data["version"], data["dry_run"]), ("cc-envcheck", "1.2.0", False))
        self.assertTrue(Path(data["wheel"]).is_file())
        code, out, err = run_cli(["build", "--project", str(self.project_dir), "--out", str(self.tmp / "dist"), "--quiet"])
        self.assertEqual((code, out, err), (0, "", ""))

    def test_exit_codes_and_diagnostics_on_stderr(self):
        code, out, err = run_cli(["build", "--project", str(self.tmp / "nowhere")])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("shipit: no pyproject.toml", err)
        code, out, err = run_cli(["install", "--target", str(self.tmp / "t")])
        self.assertEqual(code, 2)
        self.assertIn("nothing to install", err)
        code, _, err = run_cli(["frobnicate"])
        self.assertEqual(code, 2)
        broken = self.tmp / "broken.whl"
        broken.write_bytes(b"not a zip")
        code, out, err = run_cli(["inspect", str(broken)])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("not a wheel file name", err)

    def test_lock_dry_run_writes_nothing_and_warns_about_version_drift(self):
        index = self.index()
        runner = fake_git([(["git", "describe"], main.CommandResult(0, "v1.3.0-0-gabc1234\n"))])
        project = main.load_project(self.project_dir)
        stderr = io.StringIO()
        output = main.Output(False, False, io.StringIO(), stderr)
        self.assertEqual(main.project_version(project, output, runner), "1.3.0")
        self.assertIn("__version__ is 1.2.0 but the git tag gives 1.3.0", stderr.getvalue())
        self.static_project()
        lock = self.tmp / "shipit.lock"
        code, out, _ = run_cli(["lock", "--project", str(self.project_dir), "--find-links", str(index), "--out", str(lock), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("would pin cc-term==0.3.1", out)
        self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()
