import errno
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

DVC_LOCK = """schema: '2.0'
stages:
  prepare:
    cmd: cp data/raw.csv prepared.csv; echo prepare >> runs.txt
    deps:
    - path: data/raw.csv
      hash: md5
      md5: 54bc7d11ceb0d713444942aebfc9fb5d
      size: 8
    outs:
    - path: prepared.csv
      hash: md5
      md5: 54bc7d11ceb0d713444942aebfc9fb5d
      size: 8
  train:
    cmd: 'cat prepared.csv > model.txt; echo {"n": 1} > metrics.json; echo train >>
      runs.txt'
    deps:
    - path: data/images
      hash: md5
      md5: b38334d2c106d28534c1062921159181.dir
      size: 10
      nfiles: 3
    outs:
    - path: model.txt
      hash: md5
      md5: 54bc7d11ceb0d713444942aebfc9fb5d
      size: 8
"""

DVC_YAML = """stages:
  train:
    cmd: cat prepared.csv params.txt > model.txt && echo train >> runs.txt
    deps:
    - prepared.csv
    - params.txt
    outs:
    - model.txt
  prepare:
    cmd: tail -n +2 data/raw.csv > prepared.csv && echo prepare >> runs.txt
    deps:
    - data/raw.csv
    outs:
    - prepared.csv
"""


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    path.write_bytes(data)
    return path


def images(root):
    write(root / "x.txt", "hello\n")
    write(root / "sub" / "y.txt", "b\n")
    write(root / "a b.txt", "1\n")
    return root


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.requests = []
        self.page_size = 2

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        url = req.full_url.split("?")[0]
        bucket_and_key = url.split("/", 3)[3]
        _, _, key = bucket_and_key.partition("/")
        key = urllib.parse.unquote(key)
        method = req.get_method()
        if method == "PUT":
            self.objects[key] = req.data
            return FakeResponse(200, b"")
        if method == "DELETE":
            self.objects.pop(key, None)
            return FakeResponse(204, b"")
        if key:
            if key not in self.objects:
                raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"<Error><Code>NoSuchKey</Code></Error>"))
            return FakeResponse(200, self.objects[key])
        query = dict(urllib.parse.parse_qsl(req.full_url.split("?", 1)[1]))
        keys = sorted(k for k in self.objects if k.startswith(query["prefix"]))
        start = int(query.get("continuation-token", "0"))
        page = keys[start:start + self.page_size]
        more = start + self.page_size < len(keys)
        xml = '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        xml += "".join(f"<Contents><Key>{k}</Key></Contents>" for k in page)
        xml += f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>"
        if more:
            xml += f"<NextContinuationToken>{start + self.page_size}</NextContinuationToken>"
        return FakeResponse(200, (xml + "</ListBucketResult>").encode())


class FakeGit:
    def __init__(self, commits):
        self.history = commits

    def commits(self):
        return list(self.history)

    def tracked_files(self, commit):
        return {path: f"{commit}:{path}" for path in self.history[commit]}

    def blob(self, sha):
        commit, path = sha.split(":", 1)
        return self.history[commit][path]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self.cleanup)
        self.repo = main.Repo.init(self.tmp / "repo")
        self.root = self.repo.root

    def cleanup(self):
        for current, dirs, files in os.walk(self.tmp):
            for name in dirs + files:
                full = os.path.join(current, name)
                if not os.path.islink(full):
                    os.chmod(full, 0o755)
        shutil.rmtree(self.tmp)


class HashingTests(Base):
    def test_file_md5_is_plain_md5_of_the_bytes(self):
        path = write(self.tmp / "raw.csv", "csv\n1,2\n")
        info, _, _ = main.hash_path(path)
        self.assertEqual(info, main.HashInfo("54bc7d11ceb0d713444942aebfc9fb5d", 8))
        self.assertEqual(main.file_md5(write(self.tmp / "empty", b"")), "d41d8cd98f00b204e9800998ecf8427e")

    def test_directory_hash_matches_dvc(self):
        info, entries, data = main.hash_tree(images(self.tmp / "images"))
        self.assertEqual(info, main.HashInfo("b38334d2c106d28534c1062921159181.dir", 10, 3))
        self.assertEqual([e.relpath for e in entries], ["a b.txt", "sub/y.txt", "x.txt"])
        self.assertEqual(data, b'[{"md5": "b026324c6904b2a9cb4b88d6d61c81d1", "relpath": "a b.txt"}, {"md5": "3b5d5c3712955042212316173ccf37be", "relpath": "sub/y.txt"}, {"md5": "b1946ac92492d2347c6235b4d2611184", "relpath": "x.txt"}]')

    def test_manifest_sorts_relpath_as_strings_like_dvc(self):
        root = self.tmp / "d2"
        for name, body in (("sub/y.txt", "1"), ("sub-z.txt", "2"), ("sub.x", "3"), ("A.txt", "4")):
            write(root / name, body)
        info, entries, _ = main.hash_tree(root)
        self.assertEqual([e.relpath for e in entries], ["A.txt", "sub-z.txt", "sub.x", "sub/y.txt"])
        self.assertEqual(info.md5, "6a8e7e1ec7b6e9d206cd98fe1d392ac9.dir")

    def test_empty_directory_and_content_not_names_or_times(self):
        (self.tmp / "e").mkdir()
        self.assertEqual(main.hash_tree(self.tmp / "e")[0], main.HashInfo("d751713988987e9331980363e24189ce.dir", 0, 0))
        first = main.hash_tree(images(self.tmp / "one"))[0]
        copy = images(self.tmp / "two")
        os.utime(copy / "x.txt", (0, 0))
        self.assertEqual(main.hash_tree(copy)[0], first)
        write(copy / "x.txt", "hello!\n")
        self.assertNotEqual(main.hash_tree(copy)[0].md5, first.md5)

    def test_pointer_text_is_byte_identical_to_dvc(self):
        self.assertEqual(main.pointer_text(main.HashInfo("54bc7d11ceb0d713444942aebfc9fb5d", 8), "raw.csv"),
                         "outs:\n- md5: 54bc7d11ceb0d713444942aebfc9fb5d\n  size: 8\n  hash: md5\n  path: raw.csv\n")
        self.assertEqual(main.pointer_text(main.HashInfo("b38334d2c106d28534c1062921159181.dir", 10, 3), "images"),
                         "outs:\n- md5: b38334d2c106d28534c1062921159181.dir\n  size: 10\n  nfiles: 3\n  hash: md5\n  path: images\n")


class YamlTests(unittest.TestCase):
    def test_reads_a_lock_file_written_by_dvc(self):
        data = main.load_yaml(DVC_LOCK)
        self.assertEqual(data["schema"], "2.0")
        self.assertEqual(data["stages"]["train"]["cmd"], 'cat prepared.csv > model.txt; echo {"n": 1} > metrics.json; echo train >> runs.txt')
        self.assertEqual(data["stages"]["train"]["deps"][0], {"path": "data/images", "hash": "md5", "md5": "b38334d2c106d28534c1062921159181.dir", "size": 10, "nfiles": 3})
        self.assertEqual(data["stages"]["prepare"]["outs"][0]["size"], 8)

    def test_round_trip_and_nested_list_mappings(self):
        value = {"schema": "2.0", "stages": {"a": {"cmd": "echo 'x' > y; true", "deps": [{"path": "a b.txt", "size": 1}], "outs": []}},
                 "metrics": [{"metrics.json": {"cache": False}}], "plain": ["s3://bucket/x", "http://127.0.0.1:9000"]}
        self.assertEqual(main.load_yaml(main.to_yaml(value)), value)
        self.assertEqual(main.load_yaml("# c\nouts:\n- md5: abc # note\n  path: 'it''s'\n"), {"outs": [{"md5": "abc", "path": "it's"}]})

    def test_rejects_malformed_yaml(self):
        with self.assertRaises(main.UsageError):
            main.load_yaml("stages:\n  a:\n    cmd: x\n  b\n")


class CacheAndAddTests(Base):
    def test_add_writes_cache_pointer_and_gitignore(self):
        images(self.root / "data" / "images")
        write(self.root / "data" / "raw.csv", "csv\n1,2\n")
        result = self.repo.add([str(self.root / "data" / "raw.csv"), str(self.root / "data" / "images")])
        self.assertEqual([r["md5"] for r in result], ["54bc7d11ceb0d713444942aebfc9fb5d", "b38334d2c106d28534c1062921159181.dir"])
        self.assertEqual((self.root / "data" / ".gitignore").read_text(), "/raw.csv\n/images\n")
        self.assertEqual(self.repo.cache.objects(), {"54bc7d11ceb0d713444942aebfc9fb5d", "b38334d2c106d28534c1062921159181.dir",
                                                     "b026324c6904b2a9cb4b88d6d61c81d1", "3b5d5c3712955042212316173ccf37be", "b1946ac92492d2347c6235b4d2611184"})
        stored = self.root / ".dvc" / "cache" / "files" / "md5" / "54" / "bc7d11ceb0d713444942aebfc9fb5d"
        self.assertEqual(stored.read_bytes(), b"csv\n1,2\n")
        self.assertEqual(stored.stat().st_mode & 0o777, 0o444)
        self.repo.add([str(self.root / "data" / "raw.csv")])
        self.assertEqual((self.root / "data" / ".gitignore").read_text(), "/raw.csv\n/images\n")
        self.assertEqual(main.load_yaml((self.root / "data" / "images.dvc").read_text())["outs"][0]["nfiles"], 3)

    def test_status_reports_modified_deleted_and_not_in_cache_like_dvc(self):
        raw = write(self.root / "raw.csv", "csv\n1,2\n")
        images(self.root / "images")
        self.repo.add([str(raw), str(self.root / "images")])
        self.assertEqual(self.repo.status(), {})
        write(self.root / "images" / "new.txt", "new\n")
        self.assertEqual(self.repo.status(), {"images.dvc": [{"changed outs": {"images": "modified"}}]})
        raw.unlink()
        self.assertEqual(self.repo.status()["raw.csv.dvc"], [{"changed outs": {"raw.csv": "deleted"}}])
        self.repo.cache.remove("b1946ac92492d2347c6235b4d2611184")
        self.assertEqual(self.repo.status()["images.dvc"], [{"changed outs": {"images": "not in cache"}}])


class CheckoutTests(Base):
    def test_checkout_restores_the_version_a_pointer_names(self):
        raw = write(self.root / "raw.csv", "v1\n")
        self.repo.add([str(raw)])
        old_pointer = (self.root / "raw.csv.dvc").read_text()
        write(raw, "v2\n")
        self.repo.add([str(raw)])
        (self.root / "raw.csv.dvc").write_text(old_pointer)
        rows = self.repo.checkout()
        self.assertEqual(raw.read_text(), "v1\n")
        self.assertEqual(rows[0]["action"], "restored")
        self.assertIn(rows[0]["links"][0], ("reflink", "copy"))
        self.assertEqual(self.repo.checkout()[0]["action"], "unchanged")
        write(raw, "edited after checkout\n")
        self.assertEqual(raw.read_text(), "edited after checkout\n")
        self.assertEqual(self.repo.cache.path(main.load_yaml(old_pointer)["outs"][0]["md5"]).read_text(), "v1\n")

    def test_link_strategies_fall_back_in_order(self):
        def no_reflink(src, dst):
            Path(dst).write_bytes(b"partial")
            raise OSError(errno.EOPNOTSUPP, "Operation not supported")

        self.repo.linkers["reflink"] = no_reflink
        raw = write(self.root / "raw.csv", "data\n")
        self.repo.add([str(raw)])
        raw.unlink()
        rows = self.repo.checkout("reflink,hardlink,copy")
        self.assertEqual(rows[0]["links"], ["hardlink"])
        cached = self.repo.cache.path(main.file_md5(raw))
        self.assertEqual(os.stat(raw).st_ino, os.stat(cached).st_ino)
        self.assertEqual(raw.stat().st_mode & 0o777, 0o444)
        raw.unlink()
        self.assertEqual(self.repo.checkout("reflink,copy")[0]["links"], ["copy"])
        self.assertEqual(raw.read_text(), "data\n")
        self.assertNotEqual(os.stat(raw).st_ino, os.stat(cached).st_ino)
        self.repo.linkers["copy"] = no_reflink
        raw.unlink()
        with self.assertRaisesRegex(main.MiniDvcError, "reflink: .*copy: "):
            self.repo.checkout("reflink,copy")
        self.assertFalse(raw.exists())
        with self.assertRaises(main.UsageError):
            self.repo.checkout("reflink,teleport")

    def test_cache_type_from_config_sets_the_default_order(self):
        (self.root / ".dvc" / "config").write_text("[cache]\n    type = hardlink,copy\n")
        self.assertEqual(self.repo.link_order(), ["hardlink", "copy"])
        (self.root / ".dvc" / "config").write_text("")
        self.assertEqual(self.repo.link_order(), ["reflink", "copy"])

    def test_directory_checkout_removes_extra_files_and_protects_unsaved_work(self):
        root = images(self.root / "images")
        self.repo.add([str(root)])
        write(root / "x.txt", "unsaved edit\n")
        write(root / "deep" / "extra.txt", "extra\n")
        (root / "sub" / "y.txt").unlink()
        with self.assertRaisesRegex(main.MiniDvcError, "not in the cache would be lost"):
            self.repo.checkout()
        self.assertEqual((root / "x.txt").read_text(), "unsaved edit\n")
        row = self.repo.checkout(force=True)[0]
        self.assertEqual((row["files_restored"], row["files_removed"]), (2, 1))
        self.assertEqual(main.hash_tree(root)[0].md5, "b38334d2c106d28534c1062921159181.dir")
        self.assertFalse((root / "deep").exists())

    def test_checkout_without_cache_names_pull(self):
        raw = write(self.root / "raw.csv", "data\n")
        self.repo.add([str(raw)])
        self.repo.cache.remove(main.file_md5(raw))
        raw.unlink()
        with self.assertRaisesRegex(main.MiniDvcError, "minidvc pull"):
            self.repo.checkout()


class SigV4Tests(unittest.TestCase):
    KEY = "AKIAIOSFODNN7EXAMPLE"
    SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

    def test_aws_documented_get_object_example(self):
        headers = main.sign_v4("GET", "https://examplebucket.s3.amazonaws.com/test.txt", {"Range": "bytes=0-9"}, main.EMPTY_SHA256,
                               self.KEY, self.SECRET, "us-east-1", 1369353600)
        self.assertEqual(headers["x-amz-date"], "20130524T000000Z")
        self.assertEqual(headers["Authorization"], "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
                         "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")

    def test_put_and_list_match_botocore(self):
        when = 1793527200
        put = main.sign_v4("PUT", "http://127.0.0.1:9000/bucket/minidvc/files/md5/ab/cdef", {}, hashlib.sha256(b"hello\n").hexdigest(),
                           self.KEY, self.SECRET, "us-east-1", when)
        self.assertTrue(put["Authorization"].endswith("Signature=7bc24ba024f8f06989c319a3651a57930e67970e242bbf4aad5dacb0298e2694"))
        listing = main.sign_v4("GET", "http://127.0.0.1:9000/bucket?continuation-token=a%2Bb%3D&list-type=2&prefix=minidvc%2Ffiles%2Fmd5%2F", {},
                               main.EMPTY_SHA256, self.KEY, self.SECRET, "us-east-1", when)
        self.assertTrue(listing["Authorization"].endswith("Signature=662d6e7b6eb85a81ad620dce063f02747e07903dcdd83cec8e2d533beb701c4f"))


class RemoteTests(Base):
    def setUp(self):
        super().setUp()
        self.raw = write(self.root / "data" / "raw.csv", "csv\n1,2\n")
        images(self.root / "data" / "images")
        self.repo.add([str(self.raw), str(self.root / "data" / "images")])

    def clone(self, name):
        other = main.Repo.init(self.tmp / name)
        for pointer in ("data/raw.csv.dvc", "data/images.dvc"):
            write(other.root / pointer, (self.root / pointer).read_text())
        shutil.copyfile(self.root / ".dvc" / "config", other.root / ".dvc" / "config")
        return other

    def test_push_and_pull_through_a_local_remote(self):
        self.repo.add_remote("store", str(self.tmp / "remote"), None, True)
        self.assertIn("['remote \"store\"']", (self.root / ".dvc" / "config").read_text())
        remote = self.repo.remote()
        pushed = self.repo.push(remote)
        self.assertEqual(len(pushed["uploaded"]), 5)
        self.assertTrue(pushed["uploaded"][-1].endswith(".dir"))
        self.assertTrue((self.tmp / "remote" / "files" / "md5" / "54" / "bc7d11ceb0d713444942aebfc9fb5d").is_file())
        self.assertEqual(self.repo.push(remote)["uploaded"], [])
        other = self.clone("clone")
        fetched = other.fetch(other.remote())
        self.assertEqual(len(fetched["downloaded"]), 5)
        other.checkout()
        self.assertEqual((other.root / "data" / "raw.csv").read_text(), "csv\n1,2\n")
        self.assertEqual(main.hash_tree(other.root / "data" / "images")[0].md5, "b38334d2c106d28534c1062921159181.dir")

    def test_s3_remote_signs_paginates_and_verifies(self):
        fake = FakeS3()
        remote = main.S3Remote("bucket", "minidvc", "http://127.0.0.1:9000", "AKIA", "secret", opener=fake, clock=lambda: 1793527200)
        pushed = self.repo.push(remote)
        self.assertEqual(len(pushed["uploaded"]), 5)
        self.assertIn("minidvc/files/md5/54/bc7d11ceb0d713444942aebfc9fb5d", fake.objects)
        first = fake.requests[0]
        self.assertEqual(first.get_method(), "GET")
        self.assertIn("list-type=2", first.full_url)
        self.assertTrue(first.get_header("Authorization").startswith("AWS4-HMAC-SHA256 Credential=AKIA/20261101/us-east-1/s3/aws4_request"))
        put = [r for r in fake.requests if r.get_method() == "PUT"][0]
        self.assertEqual(put.get_header("X-amz-content-sha256"), hashlib.sha256(put.data).hexdigest())
        self.assertEqual(put.get_header("Content-type"), "application/octet-stream")
        self.assertIn("content-type;host", put.get_header("Authorization"))
        self.assertEqual(remote.keys(), set(pushed["uploaded"]))
        self.assertGreater(sum(1 for r in fake.requests if "continuation-token" in r.full_url), 0)
        fake.objects["minidvc/files/md5/54/bc7d11ceb0d713444942aebfc9fb5d"] = b"tampered"
        other = self.clone("clone")
        with self.assertRaisesRegex(main.MiniDvcError, "corrupt"):
            other.fetch(remote)
        del fake.objects["minidvc/files/md5/54/bc7d11ceb0d713444942aebfc9fb5d"]
        with self.assertRaisesRegex(main.MiniDvcError, "missing from remote"):
            other.fetch(remote)

    def test_remote_config_reads_dvc_syntax_and_credentials(self):
        (self.root / ".dvc" / "config").write_text("[core]\n    remote = store\n['remote \"store\"']\n    url = s3://bucket/dvcstore\n    endpointurl = http://127.0.0.1:9000\n")
        remote = self.repo.remote(env={"AWS_ACCESS_KEY_ID": "k", "AWS_SECRET_ACCESS_KEY": "s"})
        self.assertEqual((remote.bucket, remote.prefix, remote.endpoint, remote.region), ("bucket", "dvcstore", "http://127.0.0.1:9000", "us-east-1"))
        with self.assertRaisesRegex(main.UsageError, "AWS_ACCESS_KEY_ID"):
            self.repo.remote(env={})
        with self.assertRaisesRegex(main.UsageError, "not configured"):
            self.repo.remote("other", env={})


class GcTests(Base):
    def pointer(self, md5, name="raw.csv", size=3):
        return main.pointer_text(main.HashInfo(md5, size), name)

    def test_gc_keeps_every_commit_and_drops_the_rest(self):
        raw = self.root / "raw.csv"
        hashes = []
        for text in ("v1\n", "v2\n", "orphan\n", "v3\n"):
            write(raw, text)
            hashes.append(self.repo.add([str(raw)])[0]["md5"])
        root = images(self.root / "images")
        dir_md5 = self.repo.add([str(root)])[0]["md5"]
        images_pointer = (self.root / "images.dvc").read_text()
        (self.root / "images.dvc").unlink()
        git = FakeGit({"c1": {"raw.csv.dvc": self.pointer(hashes[0])}, "c2": {"raw.csv.dvc": self.pointer(hashes[1]), "images.dvc": images_pointer}})
        dry = self.repo.gc(True, dry_run=True, git=git)
        self.assertEqual(dry["removed_from_cache"], [hashes[2]])
        self.assertIn(hashes[2], self.repo.cache.objects())
        result = self.repo.gc(True, git=git)
        self.assertEqual(result["commits"], 2)
        self.assertEqual(self.repo.cache.objects(), {hashes[0], hashes[1], hashes[3], dir_md5, "b026324c6904b2a9cb4b88d6d61c81d1",
                                                     "3b5d5c3712955042212316173ccf37be", "b1946ac92492d2347c6235b4d2611184"})
        workspace_only = self.repo.gc(False)
        self.assertEqual(self.repo.cache.objects(), {hashes[3]})
        self.assertEqual(len(workspace_only["removed_from_cache"]), 6)

    def test_gc_cloud_deletes_from_the_remote_too(self):
        raw = self.root / "raw.csv"
        remote = main.LocalRemote(self.tmp / "remote")
        old = self.repo.add([str(write(raw, "old\n"))])[0]["md5"]
        self.repo.push(remote)
        new = self.repo.add([str(write(raw, "new\n"))])[0]["md5"]
        self.repo.push(remote)
        self.assertEqual(remote.keys(), {old, new})
        result = self.repo.gc(False, remote)
        self.assertEqual((result["removed_from_cache"], result["removed_from_remote"]), ([old], [old]))
        self.assertEqual(remote.keys(), {new})

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_reader_finds_pointers_in_every_reachable_commit(self):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")

        def git(*args):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, env=env)

        git("init", "-q")
        raw = self.root / "data" / "raw.csv"
        kept = []
        for text in ("one\n", "two\n"):
            kept.append(self.repo.add([str(write(raw, text))])[0]["md5"])
            git("add", "data/raw.csv.dvc", "data/.gitignore")
            git("commit", "-q", "-m", text.strip())
        dropped = self.repo.add([str(write(raw, "three\n"))])[0]["md5"]
        git("checkout", "-q", "--", "data/raw.csv.dvc")
        result = self.repo.gc(True)
        self.assertEqual(result["commits"], 2)
        self.assertEqual(result["removed_from_cache"], [dropped])
        self.assertEqual(self.repo.cache.objects(), set(kept))


class ReproTests(Base):
    def setUp(self):
        super().setUp()
        write(self.root / "dvc.yaml", DVC_YAML)
        write(self.root / "data" / "raw.csv", "id,v\n1,2\n")
        write(self.root / "params.txt", "lr=0.1\n")
        self.ran = []

    def runner(self, cmd, shell, cwd):
        self.assertTrue(shell)
        root = Path(cwd)
        if cmd.startswith("tail"):
            write(root / "prepared.csv", (root / "data" / "raw.csv").read_text().split("\n", 1)[1])
            self.ran.append("prepare")
        else:
            write(root / "model.txt", (root / "prepared.csv").read_text() + (root / "params.txt").read_text())
            self.ran.append("train")
        return SimpleNamespace(returncode=0)

    def repro(self):
        self.ran = []
        result = self.repo.repro(runner=self.runner)
        self.assertEqual([r["stage"] for r in result if r["ran"]], self.ran)
        return self.ran

    def test_only_affected_stages_rerun(self):
        self.assertEqual([s.name for s in self.repo.stages()], ["prepare", "train"])
        self.assertEqual(self.repro(), ["prepare", "train"])
        self.assertEqual(self.repro(), [])
        write(self.root / "params.txt", "lr=0.2\n")
        self.assertEqual(self.repro(), ["train"])
        write(self.root / "data" / "raw.csv", "id,v\n1,3\n")
        self.assertEqual(self.repro(), ["prepare", "train"])
        write(self.root / "data" / "raw.csv", "id,w\n1,3\n")
        self.assertEqual(self.repro(), ["prepare"])
        (self.root / "model.txt").unlink()
        self.assertEqual(self.repro(), ["train"])

    def test_lock_file_records_deps_and_outs_like_dvc(self):
        self.repro()
        lock = main.load_yaml((self.root / "dvc.lock").read_text())
        self.assertEqual(lock["schema"], "2.0")
        self.assertEqual(list(lock["stages"]), ["prepare", "train"])
        train = lock["stages"]["train"]
        self.assertEqual([d["path"] for d in train["deps"]], ["params.txt", "prepared.csv"])
        self.assertEqual(train["outs"][0], {"path": "model.txt", "hash": "md5", "md5": main.file_md5(self.root / "model.txt"), "size": 11})
        self.assertTrue(self.repo.cache.has(train["outs"][0]["md5"]))
        self.assertEqual((self.root / ".gitignore").read_text(), "/prepared.csv\n/model.txt\n")
        self.assertEqual(self.repo.status(), {})
        write(self.root / "params.txt", "lr=0.3\n")
        self.assertEqual(self.repo.status(), {"train": [{"changed deps": {"params.txt": "modified"}}]})

    def test_changed_command_and_cycles(self):
        self.repro()
        write(self.root / "dvc.yaml", DVC_YAML.replace("echo train", "echo TRAIN"))
        self.assertEqual(self.repo.stage_status()["train"], ["changed command"])
        write(self.root / "dvc.yaml", "stages:\n  a:\n    cmd: x\n    deps:\n    - b.txt\n    outs:\n    - a.txt\n  b:\n    cmd: y\n    deps:\n    - a.txt\n    outs:\n    - b.txt\n")
        with self.assertRaisesRegex(main.UsageError, "cycle"):
            self.repo.stages()


class CliTests(Base):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_json_output_and_exit_codes(self):
        images(self.root / "images")
        code, out, _ = self.run_cli("-C", str(self.root), "--json", "add", "images")
        self.assertEqual((code, json.loads(out)[0]["nfiles"]), (0, 3))
        code, out, _ = self.run_cli("-C", str(self.root), "--json", "hash", "images")
        self.assertEqual(json.loads(out)["md5"], "b38334d2c106d28534c1062921159181.dir")
        self.assertEqual(self.run_cli("-C", str(self.root), "status", "-q")[0], 0)
        write(self.root / "images" / "x.txt", "changed\n")
        self.assertEqual(self.run_cli("-C", str(self.root), "status", "-q")[0], 1)
        code, out, _ = self.run_cli("-C", str(self.root), "status")
        self.assertIn("modified:", out)
        code, _, err = self.run_cli("-C", str(self.tmp), "status")
        self.assertEqual(code, 2)
        self.assertIn("minidvc init", err)
        self.assertEqual(self.run_cli("-C", str(self.root), "push")[0], 2)
        self.assertEqual(self.run_cli("bogus")[0], 2)


if __name__ == "__main__":
    unittest.main()
