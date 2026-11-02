import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zlib
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
sys.path.insert(0, CODE)

import main  # noqa: E402

EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
HELLO = "ce013625030ba8dba906f756967f9e9ca394464a"
EMPTY_BLOB_SHA256 = "473a0f4c3be8a93681a267e3b1e9a7dcda1185436fe141f7749120a303721813"
NOW = 1767600000
ENV = {
    "GIT_AUTHOR_NAME": "Ada Lovelace",
    "GIT_AUTHOR_EMAIL": "ada@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-05T09:00:00+01:00",
    "GIT_COMMITTER_NAME": "Grace Hopper",
    "GIT_COMMITTER_EMAIL": "grace@example.invalid",
    "GIT_COMMITTER_DATE": "2026-01-05T09:30:00+01:00",
}


def run(argv, stdin=b"", env=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(argv, stdin=io.BytesIO(stdin), env=ENV if env is None else env, now=NOW)
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        empty = os.path.join(self.dir, "empty-gitconfig")
        open(empty, "w").close()
        patcher = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": empty, "GIT_CONFIG_NOSYSTEM": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = os.path.join(self.dir, "work")
        self.repo, _ = main.init_repo(self.work)
        self.ident = main.Ident("Grace Hopper", "grace@example.invalid", NOW, "+0100")

    def write(self, rel, data, executable=False):
        path = os.path.join(self.work, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data if isinstance(data, bytes) else data.encode())
        os.chmod(path, 0o755 if executable else 0o644)
        return path

    def commit(self, message, env=None):
        return main.commit_worktree(self.repo, message, env or ENV, NOW)[0]

    def entries(self, tree):
        return main.parse_tree(main.read_object(self.repo, tree)[1])


class ObjectTests(Base):
    def test_ids_hash_the_header_and_the_body_together(self):
        self.assertEqual(main.frame("blob", b"hello\n"), b"blob 6\x00hello\n")
        self.assertEqual(main.hash_object("blob", b"hello\n"), HELLO)
        self.assertEqual(main.hash_object("blob", b""), EMPTY_BLOB)
        self.assertEqual(main.hash_object("tree", b""), EMPTY_TREE)
        self.assertEqual(main.hash_object("blob", b"", "sha256"), EMPTY_BLOB_SHA256)
        self.assertNotEqual(main.hash_object("blob", b"x"), main.hash_object("tree", b"x"))

    def test_a_loose_object_is_zlib_of_header_and_body_written_once_read_only(self):
        oid = main.write_object(self.repo, "blob", b"hello\n")
        path = os.path.join(self.repo.git_dir, "objects", "ce", HELLO[2:])
        self.assertEqual(oid, HELLO)
        with open(path, "rb") as handle:
            self.assertEqual(zlib.decompress(handle.read()), b"blob 6\x00hello\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode) & 0o222, 0)
        self.assertEqual(main.write_object(self.repo, "blob", b"hello\n"), oid)
        self.assertEqual(main.loose_objects(self.repo), [HELLO])
        self.assertEqual(main.read_object(self.repo, oid), ("blob", b"hello\n"))
        self.assertEqual(os.listdir(os.path.dirname(path)), [HELLO[2:]])

    def test_bad_headers_empty_files_and_broken_zlib_are_named(self):
        for raw, needle in ((b"blob 6hello\n", "no NUL"), (b"blub 1\x00x", "does not start"), (b"blob 7\x00hello\n", "says 7 bytes"), (b"blob 06\x00hello\n", "bad size")):
            with self.assertRaises(main.GitError) as caught:
                main.unframe(raw)
            self.assertIn(needle, str(caught.exception))
        fake = "ab" + "0" * 38
        path = main.object_path(self.repo, fake)
        os.makedirs(os.path.dirname(path))
        open(path, "wb").close()
        with self.assertRaisesRegex(main.GitError, "is empty"):
            main.read_object(self.repo, fake)
        with open(path, "wb") as handle:
            handle.write(b"not zlib")
        with self.assertRaisesRegex(main.GitError, "is corrupt"):
            main.read_object(self.repo, fake)
        with self.assertRaises(main.ObjectMissing):
            main.read_object(self.repo, "cd" + "0" * 38)


class TreeTests(Base):
    def test_trees_sort_as_if_directory_names_ended_with_a_slash(self):
        entries = [main.TreeEntry(main.MODE_TREE, "foo", EMPTY_TREE)] + [main.TreeEntry(main.MODE_FILE, n, EMPTY_BLOB) for n in ("foo0", "foo.c", "foo-bar")]
        body = main.encode_tree(entries)
        self.assertEqual([e.name for e in main.parse_tree(body)], ["foo-bar", "foo.c", "foo", "foo0"])
        self.assertIn(b"40000 foo\x00", body)
        self.assertNotIn(b"040000", body)
        self.assertEqual(main.ls_tree_line(entries[0]), f"040000 tree {EMPTY_TREE}\tfoo")
        naive = main.encode_tree(sorted(entries, key=lambda e: e.name), keep_order=True)
        self.assertNotEqual(main.hash_object("tree", naive), main.hash_object("tree", body))
        self.assertIn("treeNotSorted", [c for c, _ in main.tree_problems(main.parse_tree(naive))])

    def test_tree_problems_use_git_fsck_message_ids(self):
        names = ["", "a/b", ".", "..", ".GIT", "dup", "dup"]
        entries = [main.TreeEntry(main.MODE_FILE, n, EMPTY_BLOB) for n in names]
        entries += [main.TreeEntry("040000", "zz", EMPTY_TREE), main.TreeEntry("100664", "zzz", EMPTY_BLOB)]
        checks = {c for c, _ in main.tree_problems(entries)}
        self.assertTrue({"emptyName", "fullPathname", "hasDot", "hasDotdot", "hasDotgit", "duplicateEntries", "zeroPaddedFilemode", "badFilemode"} <= checks, checks)
        self.assertEqual(main.tree_problems(main.parse_tree(main.encode_tree([main.TreeEntry(main.MODE_FILE, "a", EMPTY_BLOB)]))), [])

    def test_write_tree_records_modes_symlinks_nesting_and_skips_empty_dirs(self):
        self.write("README.md", "# demo\n")
        self.write("bin/run.sh", "#!/bin/sh\necho hi\n", executable=True)
        self.write("src/app/util/helpers.py", "X = 1\n")
        os.symlink("README.md", os.path.join(self.work, "latest"))
        os.makedirs(os.path.join(self.work, "empty"))
        tree = main.write_tree_from_dir(self.repo, self.work)
        top = {e.name: e for e in self.entries(tree)}
        self.assertEqual(sorted(top), ["README.md", "bin", "latest", "src"])
        self.assertEqual(top["latest"].mode, main.MODE_LINK)
        self.assertEqual(main.read_object(self.repo, top["latest"].oid), ("blob", b"README.md"))
        self.assertEqual(top["bin"].mode, main.MODE_TREE)
        self.assertEqual(self.entries(top["bin"].oid)[0].mode, main.MODE_EXEC)
        self.assertEqual(top["README.md"].mode, main.MODE_FILE)
        paths = [p for _, p in main.walk_tree(self.repo, tree)]
        self.assertIn("src/app/util/helpers.py", paths)
        kept = {e.name: e for e in self.entries(main.write_tree_from_dir(self.repo, self.work, keep_empty=True))}
        self.assertEqual((kept["empty"].mode, kept["empty"].oid), (main.MODE_TREE, EMPTY_TREE))
        bare = os.path.join(self.dir, "only-empty")
        os.makedirs(os.path.join(bare, "a", "b"))
        self.assertEqual(main.write_tree_from_dir(self.repo, bare), EMPTY_TREE)

    def test_an_embedded_repository_becomes_a_gitlink_to_its_head(self):
        nested_path = os.path.join(self.work, "vendor", "lib")
        nested, _ = main.init_repo(nested_path)
        os.makedirs(os.path.join(self.work, "vendor"), exist_ok=True)
        with self.assertRaisesRegex(main.GitError, "no commit checked out"):
            main.write_tree_from_dir(self.repo, self.work)
        with open(os.path.join(nested_path, "lib.txt"), "w") as handle:
            handle.write("lib\n")
        head = main.commit_worktree(nested, "lib", ENV, NOW)[0]
        vendor = {e.name: e for e in self.entries(main.write_tree_from_dir(self.repo, self.work))}["vendor"]
        gitlink = self.entries(vendor.oid)[0]
        self.assertEqual((gitlink.mode, gitlink.name, gitlink.oid, gitlink.kind), (main.MODE_GITLINK, "lib", head, "commit"))


class CommitTests(Base):
    def test_dates_become_unix_time_plus_the_original_zone(self):
        self.assertEqual(main.parse_date("@1767600000 +0100"), (NOW, "+0100"))
        self.assertEqual(main.parse_date("1767600000"), (NOW, "+0000"))
        self.assertEqual(main.parse_date("2026-01-05T09:00:00+01:00"), (NOW, "+0100"))
        self.assertEqual(main.parse_date("2026-01-05 08:00:00 Z"), (NOW, "+0000"))
        self.assertEqual(main.parse_date("2026-02-01T08:00:00-05:00"), (1769950800, "-0500"))
        self.assertEqual(main.parse_date("2026-01-05T13:30+0530"), (NOW, "+0530"))
        self.assertEqual(main.format_date(1769950800, "-0500"), "Sun Feb 1 08:00:00 2026 -0500")
        self.assertEqual(main.format_date(NOW, "+0530"), "Mon Jan 5 13:30:00 2026 +0530")
        with self.assertRaisesRegex(main.GitError, "cannot parse date"):
            main.parse_date("last tuesday")

    def test_a_commit_is_text_headers_a_blank_line_and_the_message(self):
        tree = main.write_object(self.repo, "tree", b"")
        first = main.make_commit(self.repo, tree, [], "first\n", ENV, NOW)
        raw = main.read_object(self.repo, first)[1]
        self.assertEqual(raw, (
            f"tree {EMPTY_TREE}\n"
            "author Ada Lovelace <ada@example.invalid> 1767600000 +0100\n"
            "committer Grace Hopper <grace@example.invalid> 1767601800 +0100\n"
            "\nfirst\n"
        ).encode())
        second = main.make_commit(self.repo, tree, [first], "second\n", ENV, NOW)
        parsed = main.parse_commit(main.read_object(self.repo, second)[1])
        self.assertEqual((parsed.tree, parsed.parents, parsed.author.tz, parsed.committer.time), (EMPTY_TREE, [first], "+0100", 1767601800))
        self.assertEqual(main.encode_commit(parsed), main.read_object(self.repo, second)[1])
        code, out, _ = run(["-C", self.work, "commit-tree", EMPTY_TREE, "-p", first, "-m", "second"])
        self.assertEqual((code, out.strip()), (0, second))
        with self.assertRaisesRegex(main.GitError, "is not a tree"):
            main.make_commit(self.repo, first, [], "x\n", ENV, NOW)

    def test_identity_comes_from_the_environment_then_the_config(self):
        with self.assertRaisesRegex(main.GitError, "author identity unknown"):
            main.make_ident(self.repo, "AUTHOR", {}, NOW)
        self.repo.config.update({"user.name": "Config <Name>", "user.email": "config@example.invalid"})
        ident = main.make_ident(self.repo, "AUTHOR", {"GIT_AUTHOR_DATE": "@1 +0000"}, NOW)
        self.assertEqual(ident.encode(), "Config Name <config@example.invalid> 1 +0000")
        self.assertEqual(main.make_ident(self.repo, "AUTHOR", ENV, NOW).name, "Ada Lovelace")
        self.assertEqual(main.make_ident(self.repo, "COMMITTER", {"GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "y"}, NOW).time, NOW)

    def test_messages_are_cleaned_and_tags_point_at_typed_objects(self):
        self.assertEqual(main.cleanup_message("\n  \nSubject   \n\n\n\nBody line\n\n"), "Subject\n\nBody line\n")
        self.assertEqual(main.complete_line("x"), "x\n")
        self.write("a.txt", "a\n")
        head = self.commit("first")
        tag = main.make_tag(self.repo, "v1.0", head, "Release 1.0", ENV, NOW)
        self.assertEqual(main.read_object(self.repo, tag), ("tag", (
            f"object {head}\ntype commit\ntag v1.0\n"
            "tagger Grace Hopper <grace@example.invalid> 1767601800 +0100\n\nRelease 1.0\n"
        ).encode()))
        self.assertEqual(main.rev_parse(self.repo, "v1.0"), tag)
        self.assertEqual(main.rev_parse(self.repo, "v1.0^{}"), head)
        self.assertEqual(main.make_tag(self.repo, "light", head, None, ENV, NOW), head)
        with self.assertRaisesRegex(main.GitError, "already exists"):
            main.make_tag(self.repo, "v1.0", head, "again", ENV, NOW)
        with self.assertRaisesRegex(main.GitError, "not a valid tag name"):
            main.make_tag(self.repo, "bad name", head, None, ENV, NOW)


class RefTests(Base):
    def test_head_is_symbolic_and_refs_fall_back_to_packed_refs(self):
        self.assertEqual(main.read_ref(self.repo, "HEAD"), "ref: refs/heads/main")
        self.assertIsNone(main.resolve_ref(self.repo, "HEAD"))
        with self.assertRaisesRegex(main.GitError, "points to refs/heads/main, which has no commit yet"):
            main.rev_parse(self.repo, "HEAD")
        blob = main.write_object(self.repo, "blob", b"x")
        with open(os.path.join(self.repo.git_dir, "packed-refs"), "w") as handle:
            handle.write(f"# pack-refs with: peeled fully-peeled sorted \n{HELLO} refs/heads/main\n{blob} refs/tags/old\n^{EMPTY_BLOB}\n")
        self.assertEqual(main.follow_symrefs(self.repo, "HEAD"), ("refs/heads/main", HELLO))
        self.assertEqual(main.list_refs(self.repo), {"refs/heads/main": HELLO, "refs/tags/old": blob})
        main.update_ref(self.repo, "HEAD", blob, self.ident)
        self.assertEqual(main.resolve_ref(self.repo, "refs/heads/main"), blob)
        main.delete_ref(self.repo, "refs/tags/old")
        with open(os.path.join(self.repo.git_dir, "packed-refs")) as handle:
            self.assertEqual(handle.read().count("\n"), 2)
        self.assertNotIn("refs/tags/old", main.list_refs(self.repo))

    def test_short_names_follow_gits_dwim_order_and_symref_loops_fail(self):
        blob_a = main.write_object(self.repo, "blob", b"a")
        blob_b = main.write_object(self.repo, "blob", b"b")
        main.update_ref(self.repo, "refs/heads/x", blob_a, self.ident)
        main.update_ref(self.repo, "refs/tags/x", blob_b, self.ident)
        self.assertEqual(main.dwim_refs(self.repo, "x"), ["refs/tags/x", "refs/heads/x"])
        self.assertEqual(main.rev_parse(self.repo, "x"), blob_b)
        self.assertEqual(main.rev_parse(self.repo, "heads/x"), blob_a)
        code, out, err = run(["-C", self.work, "rev-parse", "x"])
        self.assertEqual((code, out.strip()), (0, blob_b))
        self.assertIn("ambiguous", err)
        for name, target in (("refs/heads/a", "refs/heads/b"), ("refs/heads/b", "refs/heads/a")):
            with open(main.ref_path(self.repo, name), "w") as handle:
                handle.write(f"ref: {target}\n")
        with self.assertRaisesRegex(main.GitError, "loop"):
            main.resolve_ref(self.repo, "refs/heads/a")

    def test_update_ref_is_a_compare_and_swap_under_a_lock_file(self):
        one = main.write_object(self.repo, "blob", b"1")
        two = main.write_object(self.repo, "blob", b"2")
        main.update_ref(self.repo, "refs/heads/main", one, self.ident, old=self.repo.zero)
        with self.assertRaisesRegex(main.GitError, "reference already exists"):
            main.update_ref(self.repo, "refs/heads/main", two, self.ident, old=self.repo.zero)
        with self.assertRaisesRegex(main.GitError, f"is at {one} but expected {two}"):
            main.update_ref(self.repo, "refs/heads/main", two, self.ident, old=two)
        path = main.ref_path(self.repo, "refs/heads/main")
        self.assertFalse(os.path.exists(path + ".lock"))
        open(path + ".lock", "w").close()
        with self.assertRaisesRegex(main.GitError, "Unable to create .*main.lock': File exists"):
            main.update_ref(self.repo, "refs/heads/main", two, self.ident)
        os.unlink(path + ".lock")
        main.update_ref(self.repo, "refs/heads/main", two, self.ident, old=one)
        with open(path) as handle:
            self.assertEqual(handle.read(), two + "\n")
        with self.assertRaisesRegex(main.GitError, "parent directories"):
            main.update_ref(self.repo, "refs/heads/main/sub", one, self.ident)

    def test_ref_names_follow_check_ref_format(self):
        for good in ("refs/heads/main", "refs/heads/feature/login", "refs/tags/v1.2.3", "HEAD"):
            self.assertIsNone(main.check_ref_format(good), good)
        for bad in ("main", "refs/heads/a..b", "refs/heads/x.lock", "refs/heads/.hidden", "refs/heads/a b", "refs/heads/a~1", "refs/heads/a^", "refs/heads/a:b",
                    "refs/heads/a?", "refs/heads/a*", "refs/heads/a[", "refs/heads/a\\b", "refs/heads/end.", "refs/heads/", "refs//x", "refs/heads/x@{1}"):
            self.assertIsNotNone(main.check_ref_format(bad), bad)

    def test_the_reflog_records_branch_and_head_and_resolves_at_n(self):
        self.write("a.txt", "1\n")
        first = self.commit("one")
        self.write("a.txt", "2\n")
        second = self.commit("two")
        head_log = main.read_reflog(self.repo, "HEAD")
        branch_log = main.read_reflog(self.repo, "refs/heads/main")
        self.assertEqual([(e.old, e.new, e.message) for e in head_log], [(self.repo.zero, first, "commit (initial): one"), (first, second, "commit: two")])
        self.assertEqual(head_log, branch_log)
        self.assertEqual(main.rev_parse(self.repo, "HEAD@{1}"), first)
        self.assertEqual(main.rev_parse(self.repo, "main@{0}"), second)
        self.assertEqual(main.rev_parse(self.repo, "@{1}"), first)
        with self.assertRaisesRegex(main.GitError, "only has 2 entries"):
            main.rev_parse(self.repo, "HEAD@{2}")
        with self.assertRaisesRegex(main.GitError, "nothing to commit"):
            self.commit("same")
        main.update_ref(self.repo, "refs/heads/main", first, self.ident, message="reset: moving to HEAD~1")
        self.assertEqual(main.read_reflog(self.repo, "HEAD")[-1].message, "reset: moving to HEAD~1")
        code, out, _ = run(["-C", self.work, "reflog", "--json"])
        rows = json.loads(out)
        self.assertEqual([(r["selector"], r["new"]) for r in rows], [("HEAD@{0}", first), ("HEAD@{1}", second), ("HEAD@{2}", first)])


class RevisionTests(Base):
    def setUp(self):
        super().setUp()
        tree = main.write_tree_from_dir(self.repo, self.write_tree_dir())
        self.tree = tree
        self.c1 = main.make_commit(self.repo, tree, [], "c1\n", ENV, NOW)
        self.c2 = main.make_commit(self.repo, tree, [self.c1], "c2\n", ENV, NOW + 1)
        self.side = main.make_commit(self.repo, tree, [self.c1], "side\n", ENV, NOW + 2)
        self.merge = main.make_commit(self.repo, tree, [self.c2, self.side], "Merge side\n", ENV, NOW + 3)
        main.update_ref(self.repo, "HEAD", self.merge, self.ident)

    def write_tree_dir(self):
        folder = os.path.join(self.dir, "content")
        os.makedirs(os.path.join(folder, "dir"))
        with open(os.path.join(folder, "dir", "file"), "w") as handle:
            handle.write("hello\n")
        return folder

    def test_suffixes_paths_and_prefixes_resolve_like_git(self):
        self.assertEqual(main.rev_parse(self.repo, "HEAD~1"), self.c2)
        self.assertEqual(main.rev_parse(self.repo, "HEAD^2"), self.side)
        self.assertEqual(main.rev_parse(self.repo, "HEAD~2"), self.c1)
        self.assertEqual(main.rev_parse(self.repo, "HEAD^2~1"), self.c1)
        self.assertEqual(main.rev_parse(self.repo, "main^{tree}"), self.tree)
        self.assertEqual(main.rev_parse(self.repo, "HEAD:dir/file"), HELLO)
        self.assertEqual(main.rev_parse(self.repo, self.merge[:7]), self.merge)
        with self.assertRaisesRegex(main.GitError, "has 2 parent"):
            main.rev_parse(self.repo, "HEAD^3")
        with self.assertRaisesRegex(main.GitError, "does not exist"):
            main.rev_parse(self.repo, "HEAD:dir/nope")
        with self.assertRaisesRegex(main.GitError, "index"):
            main.rev_parse(self.repo, ":dir/file")
        fan = os.path.join(self.repo.git_dir, "objects", "ab")
        os.makedirs(fan, exist_ok=True)
        for tail in ("cd" + "1" * 36, "cd" + "2" * 36):
            open(os.path.join(fan, tail), "w").close()
        with self.assertRaisesRegex(main.GitError, "ambiguous"):
            main.rev_parse(self.repo, "abcd")

    def test_log_walks_first_parents_and_prints_gits_medium_format(self):
        walked = [oid for oid, _ in main.first_parent_log(self.repo, self.merge)]
        self.assertEqual(walked, [self.merge, self.c2, self.c1])
        self.assertEqual(len(main.first_parent_log(self.repo, self.merge, limit=2)), 2)
        text = main.render_log(main.first_parent_log(self.repo, self.merge, 1), oneline=False)
        self.assertEqual(text.splitlines(), [
            f"commit {self.merge}",
            f"Merge: {self.c2[:7]} {self.side[:7]}",
            "Author: Ada Lovelace <ada@example.invalid>",
            "Date:   Mon Jan 5 09:00:00 2026 +0100",
            "",
            "    Merge side",
        ])
        code, out, _ = run(["-C", self.work, "log", "--json"])
        self.assertEqual([c["oid"] for c in json.loads(out)], walked)
        self.assertEqual(json.loads(out)[0]["author"]["date"], "2026-01-05T09:00:00+01:00")


class CheckoutTests(Base):
    def test_checkout_writes_files_modes_and_symlinks_into_an_empty_dir(self):
        self.write("bin/run.sh", "#!/bin/sh\n", executable=True)
        self.write("docs/v1/guide.md", "guide\n")
        os.symlink("v1/guide.md", os.path.join(self.work, "docs", "latest"))
        head = self.commit("snapshot")
        out = os.path.join(self.dir, "out")
        self.assertEqual(main.checkout_tree(self.repo, main.peel(self.repo, head, "tree"), out), 3)
        self.assertTrue(os.stat(os.path.join(out, "bin", "run.sh")).st_mode & stat.S_IXUSR)
        self.assertFalse(os.stat(os.path.join(out, "docs", "v1", "guide.md")).st_mode & stat.S_IXUSR)
        self.assertEqual(os.readlink(os.path.join(out, "docs", "latest")), "v1/guide.md")
        with open(os.path.join(out, "docs", "latest")) as handle:
            self.assertEqual(handle.read(), "guide\n")
        with self.assertRaisesRegex(main.GitError, "not empty"):
            main.checkout_tree(self.repo, main.peel(self.repo, head, "tree"), out)

    def test_checkout_refuses_names_that_escape_or_overwrite_git(self):
        blob = main.write_object(self.repo, "blob", b"[core]\n\tfsmonitor = touch /tmp/pwned\n")
        for name, check in ((".git", "named '.git'"), ("..", "named '..'"), (".Git", "named '.Git'")):
            tree = main.write_object(self.repo, "tree", main.encode_tree([main.TreeEntry(main.MODE_FILE, name, blob)]))
            with self.assertRaisesRegex(main.GitError, check):
                main.checkout_tree(self.repo, tree, os.path.join(self.dir, "evil-" + str(len(name))))
        link = main.write_object(self.repo, "blob", self.dir.encode())
        twisted = main.encode_tree([main.TreeEntry(main.MODE_LINK, "a", link), main.TreeEntry(main.MODE_TREE, "a", EMPTY_TREE)], keep_order=True)
        tree = main.write_object(self.repo, "tree", twisted)
        with self.assertRaisesRegex(main.GitError, "appears twice"):
            main.checkout_tree(self.repo, tree, os.path.join(self.dir, "twisted"))


class FsckTests(Base):
    def test_fsck_is_clean_then_reports_dangling_mismatched_and_missing_objects(self):
        self.write("a.txt", "a\n")
        self.write("b.txt", "b\n")
        self.commit("one")
        report = main.fsck(self.repo)
        self.assertEqual((report.problems, report.dangling, report.loose, report.reachable), ([], [], 4, 4))
        dangling = main.write_object(self.repo, "blob", b"lost work\n")
        report = main.fsck(self.repo)
        self.assertEqual(report.dangling, [(dangling, "blob")])
        self.assertEqual(run(["-C", self.work, "fsck"])[0], 0)
        a_blob = main.hash_object("blob", b"a\n")
        b_blob = main.hash_object("blob", b"b\n")
        a_path = main.object_path(self.repo, a_blob)
        os.chmod(a_path, 0o644)
        with open(main.object_path(self.repo, b_blob), "rb") as src, open(a_path, "wb") as dst:
            dst.write(src.read())
        os.unlink(main.object_path(self.repo, b_blob))
        checks = {(p.oid, p.check) for p in main.fsck(self.repo).problems}
        self.assertIn((a_blob, "hashMismatch"), checks)
        self.assertIn((b_blob, "missingObject"), checks)
        code, out, _ = run(["-C", self.work, "fsck", "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])
        self.assertIn(f"missing blob {b_blob}", run(["-C", self.work, "fsck"])[1])

    def test_fsck_reports_unsorted_trees_bad_zones_and_warnings_only_fail_when_strict(self):
        entries = [main.TreeEntry(main.MODE_TREE, "foo", EMPTY_TREE), main.TreeEntry(main.MODE_FILE, "foo.c", EMPTY_BLOB)]
        main.write_object(self.repo, "tree", b"")
        main.write_object(self.repo, "blob", b"")
        bad_tree = main.write_object(self.repo, "tree", main.encode_tree(entries, keep_order=True))
        bad_commit = main.write_object(self.repo, "commit", f"tree {EMPTY_TREE}\nauthor A <a@x> 1 +01\ncommitter A <a@x> 1 +0100\n\nx\n".encode())
        checks = {(p.oid, p.check) for p in main.fsck(self.repo).problems}
        self.assertIn((bad_tree, "treeNotSorted"), checks)
        self.assertIn((bad_commit, "badTimezone"), checks)
        warn_repo, _ = main.init_repo(os.path.join(self.dir, "warn"))
        main.write_object(warn_repo, "blob", b"")
        padded = main.write_object(warn_repo, "tree", b"0100644 a\x00" + bytes.fromhex(EMPTY_BLOB))
        report = main.fsck(warn_repo)
        self.assertEqual([(p.oid, p.check, p.severity) for p in report.problems], [(padded, "zeroPaddedFilemode", "warning")])
        self.assertEqual((report.errors(False), len(report.errors(True))), ([], 1))
        code, _, err = run(["-C", self.work, "hash-object", "-t", "commit", "--stdin"], stdin=b"tree nope\n\nx\n")
        self.assertEqual(code, 2)
        self.assertIn("--literally", err)


class CliTests(Base):
    def test_plumbing_commands_print_ids_types_sizes_and_json(self):
        code, out, _ = run(["-C", self.work, "hash-object", "-w", "--stdin"], stdin=b"hello\n")
        self.assertEqual((code, out.strip()), (0, HELLO))
        self.assertEqual(run(["-C", self.work, "cat-file", "-t", HELLO[:6]])[1], "blob\n")
        self.assertEqual(run(["-C", self.work, "cat-file", "-s", HELLO])[1], "6\n")
        self.assertEqual(run(["-C", self.work, "cat-file", "-p", HELLO])[1], "hello\n")
        self.assertEqual(json.loads(run(["-C", self.work, "cat-file", "-p", HELLO, "--json"])[1]), {"oid": HELLO, "type": "blob", "size": 6, "text": "hello\n"})
        self.assertEqual(run(["-C", self.work, "cat-file", "-e", HELLO])[0], 0)
        self.assertEqual(run(["-C", self.work, "cat-file", "-e", "0" * 40])[0], 1)
        code, out, _ = run(["-C", self.work, "cat-file", "--batch-check"], stdin=f"{HELLO}\nnope\n".encode())
        self.assertEqual(out.splitlines(), [f"{HELLO} blob 6", "nope missing"])
        self.assertEqual(run(["hash-object", "--stdin", "--object-format", "sha256"], stdin=b"")[1].strip(), EMPTY_BLOB_SHA256)
        lines = f"100644 blob {HELLO}\thello.txt\n040000 tree {EMPTY_TREE}\tempty\n"
        main.write_object(self.repo, "tree", b"")
        code, out, _ = run(["-C", self.work, "mktree"], stdin=lines.encode())
        tree = out.strip()
        self.assertEqual(run(["-C", self.work, "ls-tree", tree])[1].splitlines(), [f"040000 tree {EMPTY_TREE}\tempty", f"100644 blob {HELLO}\thello.txt"])
        self.assertEqual(run(["-C", self.work, "mktree"], stdin=f"100644 blob {'1' * 40}\tx\n".encode())[0], 2)
        counts = json.loads(run(["-C", self.work, "count-objects", "--json"])[1])
        self.assertEqual((counts["count"], counts["in-pack"], counts["packs"]), (3, 0, 0))

    def test_porcelain_flow_and_exit_codes(self):
        self.write("a.txt", "a\n")
        code, out, _ = run(["-C", self.work, "commit", "-m", "first"])
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^\[main \(root-commit\) [0-9a-f]{7}\] first")
        self.assertEqual(run(["-C", self.work, "commit", "-m", "again"])[0], 2)
        code, out, _ = run(["-C", self.work, "show-ref", "--head", "--json"])
        self.assertEqual(set(json.loads(out)), {"HEAD", "refs/heads/main"})
        self.assertEqual(run(["-C", self.work, "symbolic-ref", "HEAD"])[1], "refs/heads/main\n")
        self.assertEqual(run(["-C", self.work, "update-ref", "refs/heads/topic", "HEAD"])[0], 0)
        self.assertEqual(run(["-C", self.work, "symbolic-ref", "HEAD", "refs/heads/topic"])[0], 0)
        self.assertEqual(main.head_target(self.repo), "refs/heads/topic")
        code, _, err = run(["-C", self.work, "update-ref", "refs/heads/x", "1" * 40])
        self.assertEqual(code, 2)
        self.assertIn("not in the object store", err)
        code, _, err = run(["-C", self.dir, "log"])
        self.assertEqual(code, 2)
        self.assertIn("not a git repository", err)
        out = os.path.join(self.dir, "co")
        self.assertEqual(run(["-C", self.work, "checkout", "HEAD", out])[0], 0)
        with open(os.path.join(out, "a.txt")) as handle:
            self.assertEqual(handle.read(), "a\n")

    def test_sha256_repositories_use_32_byte_ids_everywhere(self):
        repo, _ = main.init_repo(os.path.join(self.dir, "s256"), algo="sha256")
        self.assertEqual(main.read_config(os.path.join(repo.git_dir, "config"))["extensions.objectformat"], "sha256")
        self.assertEqual((repo.algo, repo.hex_len), ("sha256", 64))
        self.assertEqual(main.write_object(repo, "blob", b""), EMPTY_BLOB_SHA256)
        with open(os.path.join(repo.work_tree, "f"), "w") as handle:
            handle.write("x\n")
        head = main.commit_worktree(repo, "first", ENV, NOW)[0]
        tree = main.peel(repo, head, "tree")
        self.assertEqual(len(main.read_object(repo, tree)[1]), len(b"100644 f\x00") + 32)
        self.assertEqual(main.parse_tree(main.read_object(repo, tree)[1], "sha256")[0].oid, main.hash_object("blob", b"x\n", "sha256"))
        self.assertEqual(main.fsck(repo).problems, [])
        self.assertEqual(main.read_reflog(repo, "HEAD")[0].old, "0" * 64)

    def test_packfiles_are_counted_from_the_idx_fanout_and_named_in_errors(self):
        pack_dir = os.path.join(self.repo.git_dir, "objects", "pack")
        with open(os.path.join(pack_dir, "pack-1.pack"), "wb") as handle:
            handle.write(b"PACK")
        with open(os.path.join(pack_dir, "pack-1.idx"), "wb") as handle:
            handle.write(b"\xfftOc" + (2).to_bytes(4, "big") + (0).to_bytes(4, "big") * 255 + (42).to_bytes(4, "big"))
        counts = main.count_objects(self.repo)
        self.assertEqual((counts["in-pack"], counts["packs"]), (42, 1))
        with self.assertRaisesRegex(main.ObjectMissing, "1 packfile"):
            main.read_object(self.repo, HELLO)


GIT = shutil.which("git")


@unittest.skipUnless(GIT, "git is not installed")
class RealGitTests(Base):
    def git(self, cwd, *args, check=True):
        env = dict(os.environ, **ENV)
        result = subprocess.run([GIT, "-c", "init.defaultBranch=main", "-c", "init.defaultRefFormat=files", *args], cwd=cwd, env=env, capture_output=True)
        if check and result.returncode != 0:
            self.fail(f"git {' '.join(args)} failed: {result.stderr.decode()}")
        return result

    def test_git_accepts_and_reads_what_minigit_writes(self):
        self.write("README.md", "# demo\n")
        self.write("bin/run.sh", "#!/bin/sh\necho run\n", executable=True)
        self.write("src/app/main.py", "print('hi')\n")
        os.symlink("../README.md", os.path.join(self.work, "src", "readme"))
        first = self.commit("Add the skeleton\n\nWith a body.")
        self.write("README.md", "# demo\nmore\n")
        second = self.commit("Extend README", env=dict(ENV, GIT_AUTHOR_DATE="2026-02-01T08:00:00-05:00"))
        main.make_tag(self.repo, "v1.0", second, "First release", ENV, NOW)
        fsck = self.git(self.work, "fsck", "--strict", "--no-progress")
        self.assertEqual(fsck.stdout.decode() + fsck.stderr.decode(), "")
        log = self.git(self.work, "log", "--format=%H%x1f%an%x1f%at%x1f%ai%x1f%cn%x1f%s").stdout.decode().splitlines()
        self.assertEqual([line.split("\x1f") for line in log], [
            [second, "Ada Lovelace", "1769950800", "2026-02-01 08:00:00 -0500", "Grace Hopper", "Extend README"],
            [first, "Ada Lovelace", "1767600000", "2026-01-05 09:00:00 +0100", "Grace Hopper", "Add the skeleton"],
        ])
        self.assertEqual(self.git(self.work, "cat-file", "-p", "HEAD:bin/run.sh").stdout, b"#!/bin/sh\necho run\n")
        self.assertEqual(self.git(self.work, "cat-file", "-t", "v1.0").stdout, b"tag\n")
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD~1", "HEAD^{tree}").stdout.decode().split(), [first, main.peel(self.repo, second, "tree")])
        modes = {line.split("\t")[1]: line.split()[0] for line in self.git(self.work, "ls-tree", "-r", "HEAD").stdout.decode().splitlines()}
        self.assertEqual(modes, {"README.md": "100644", "bin/run.sh": "100755", "src/app/main.py": "100644", "src/readme": "120000"})
        self.git(self.work, "reset", "-q")
        self.assertEqual(self.git(self.work, "status", "--porcelain").stdout, b"")

    def test_minigit_reads_every_object_git_writes_and_computes_the_same_ids(self):
        repo_dir = os.path.join(self.dir, "real")
        self.git(self.dir, "init", "-q", "--object-format=sha1", repo_dir)
        for name, text in (("a.txt", "a\n"), ("d/x", "x\n")):
            os.makedirs(os.path.dirname(os.path.join(repo_dir, name)), exist_ok=True)
            with open(os.path.join(repo_dir, name), "w") as handle:
                handle.write(text)
        os.symlink("a.txt", os.path.join(repo_dir, "link"))
        self.git(repo_dir, "add", "-A")
        self.git(repo_dir, "commit", "-q", "-m", "one")
        self.git(repo_dir, "checkout", "-q", "-b", "side")
        with open(os.path.join(repo_dir, "side.txt"), "w") as handle:
            handle.write("side\n")
        self.git(repo_dir, "add", "side.txt")
        self.git(repo_dir, "commit", "-q", "-m", "side")
        self.git(repo_dir, "checkout", "-q", "main")
        self.git(repo_dir, "merge", "-q", "--no-ff", "side", "-m", "merge side")
        self.git(repo_dir, "tag", "-a", "v1", "-m", "release")
        listing = self.git(repo_dir, "cat-file", "--batch-all-objects", "--batch-check").stdout.decode()
        ids = [line.split()[0] for line in listing.splitlines()]
        code, out, _ = run(["-C", repo_dir, "cat-file", "--batch-check"], stdin="\n".join(ids).encode())
        self.assertEqual(sorted(out.splitlines()), sorted(listing.splitlines()))
        repo = main.find_repo(repo_dir)
        report = main.fsck(repo)
        self.assertEqual((report.problems, report.dangling, report.reachable), ([], [], len(ids)))
        self.assertEqual(main.write_tree_from_dir(repo, repo_dir), self.git(repo_dir, "write-tree").stdout.decode().strip())
        firsts = self.git(repo_dir, "log", "--first-parent", "--format=%H").stdout.decode().split()
        self.assertEqual([oid for oid, _ in main.first_parent_log(repo, main.rev_parse(repo, "v1"))], firsts)
        self.assertEqual(main.rev_parse(repo, "HEAD^2"), main.rev_parse(repo, "side"))
        tree = main.peel(repo, main.rev_parse(repo, "HEAD"), "tree")
        commit = self.git(repo_dir, "commit-tree", tree, "-m", "same input").stdout.decode().strip()
        self.assertEqual(main.make_commit(repo, tree, [], "same input\n", ENV, NOW), commit)

    def test_git_fsck_names_the_tree_minigit_wrote_in_the_wrong_order(self):
        main.write_object(self.repo, "tree", b"")
        main.write_object(self.repo, "blob", b"")
        entries = [main.TreeEntry(main.MODE_TREE, "foo", EMPTY_TREE), main.TreeEntry(main.MODE_FILE, "foo.c", EMPTY_BLOB)]
        bad = main.write_object(self.repo, "tree", main.encode_tree(entries, keep_order=True))
        good = main.write_object(self.repo, "tree", main.encode_tree(entries))
        result = self.git(self.work, "fsck", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"error in tree {bad}: treeNotSorted", result.stdout.decode() + result.stderr.decode())
        listing = f"040000 tree {EMPTY_TREE}\tfoo\n100644 blob {EMPTY_BLOB}\tfoo.c\n"
        made = subprocess.run([GIT, "mktree"], cwd=self.work, input=listing.encode(), capture_output=True, env=dict(os.environ, **ENV))
        self.assertEqual(made.stdout.decode().strip(), good)


if __name__ == "__main__":
    unittest.main()
