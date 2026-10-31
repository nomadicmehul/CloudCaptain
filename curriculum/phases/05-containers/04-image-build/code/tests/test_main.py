import contextlib
import gzip
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    if mode is not None:
        os.chmod(path, mode)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mkimage-")
        self.addCleanup(shutil.rmtree, self.root)
        self.base = os.path.join(self.root, "base")
        self.app = os.path.join(self.root, "app")
        write(os.path.join(self.base, "bin", "hello"), "#!/bin/sh\necho hello\n", 0o755)
        write(os.path.join(self.base, "etc", "motd"), "welcome\n", 0o644)
        os.makedirs(os.path.join(self.base, "var", "empty"))
        os.symlink("hello", os.path.join(self.base, "bin", "hi"))
        write(os.path.join(self.app, "app", "hello.txt"), "hello from a hand-built image\n", 0o644)

    def build(self, **kwargs):
        options = dict(sources=[self.base, self.app], tag="demo/hand:1", entrypoint=["/bin/hello"], env=["GREETING=hi"], labels={"org.opencontainers.image.title": "demo"}, deletes=["etc/motd"])
        options.update(kwargs)
        return main.build(**options)


class LayerTests(Fixture):
    def test_layer_tar_is_sorted_normalised_and_reproducible(self):
        first = main.layer_tar(self.base)
        second = main.layer_tar(self.base)
        self.assertEqual(first, second)
        with tarfile.open(fileobj=io.BytesIO(first)) as tar:
            members = tar.getmembers()
        names = [m.name for m in members]
        self.assertEqual(names, ["bin", "bin/hello", "bin/hi", "etc", "etc/motd", "var", "var/empty"])
        self.assertTrue(all(m.mtime == 0 and m.uid == 0 and m.gid == 0 and m.uname == "" for m in members))
        by_name = {m.name: m for m in members}
        self.assertEqual(by_name["bin/hello"].mode & 0o777, 0o755)
        self.assertTrue(by_name["bin/hi"].issym())
        self.assertEqual(by_name["bin/hi"].linkname, "hello")
        self.assertTrue(by_name["var/empty"].isdir())

    def test_epoch_and_owners_are_options(self):
        with tarfile.open(fileobj=io.BytesIO(main.layer_tar(self.base, epoch=1700000000, keep_owners=True))) as tar:
            member = tar.getmember("etc/motd")
        self.assertEqual(member.mtime, 1700000000)
        self.assertEqual(member.uid, os.getuid())

    def test_two_digests_per_layer_and_gzip_reproducibility(self):
        layer = main.build_layer(self.app)
        again = main.build_layer(self.app)
        self.assertEqual(layer.gz, again.gz)
        self.assertEqual(layer.diff_id, "sha256:" + hashlib.sha256(layer.tar).hexdigest())
        self.assertEqual(layer.digest, "sha256:" + hashlib.sha256(layer.gz).hexdigest())
        self.assertNotEqual(layer.diff_id, layer.digest)
        self.assertEqual(gzip.decompress(layer.gz), layer.tar)
        self.assertEqual(layer.created_by, "mkimage --layer app")

    def test_delete_adds_a_whiteout_to_the_layer(self):
        layer = main.build_layer(self.app, deletes=["etc/motd", "/usr/share/doc"])
        with tarfile.open(fileobj=io.BytesIO(layer.tar)) as tar:
            names = tar.getnames()
        self.assertIn("etc/.wh.motd", names)
        self.assertIn("usr/share/.wh.doc", names)
        self.assertEqual(layer.created_by, "mkimage --layer app --delete /usr/share/doc --delete etc/motd")

    def test_chain_ids_follow_the_oci_definition(self):
        a, b, c = ("sha256:" + hashlib.sha256(x).hexdigest() for x in (b"a", b"b", b"c"))
        chain = main.chain_ids([a, b, c])
        self.assertEqual(chain[0], a)
        self.assertEqual(chain[1], "sha256:" + hashlib.sha256(f"{a} {b}".encode()).hexdigest())
        self.assertEqual(chain[2], "sha256:" + hashlib.sha256(f"{chain[1]} {c}".encode()).hexdigest())


class ImageTests(Fixture):
    def test_config_manifest_and_index_fit_together(self):
        image = self.build(arch="amd64")
        config = json.loads(image.config)
        self.assertEqual(config["rootfs"]["diff_ids"], [layer.diff_id for layer in image.layers])
        self.assertEqual(config["config"]["Entrypoint"], ["/bin/hello"])
        self.assertEqual(config["config"]["Env"], ["GREETING=hi"])
        self.assertEqual(config["config"]["Labels"], {"org.opencontainers.image.title": "demo"})
        self.assertEqual((config["architecture"], config["os"], config["created"]), ("amd64", "linux", "1970-01-01T00:00:00Z"))
        self.assertEqual([h["created_by"] for h in config["history"]], ["mkimage --layer base", "mkimage --layer app --delete etc/motd"])
        manifest = json.loads(image.manifest)
        self.assertEqual(manifest["config"], {"mediaType": main.CONFIG_TYPE, "digest": image.image_id, "size": len(image.config)})
        self.assertEqual([l["digest"] for l in manifest["layers"]], [layer.digest for layer in image.layers])
        self.assertEqual([l["size"] for l in manifest["layers"]], [layer.size for layer in image.layers])
        index = json.loads(image.index)
        self.assertEqual(index["manifests"][0]["digest"], image.manifest_digest)
        self.assertEqual(index["manifests"][0]["annotations"], {"io.containerd.image.name": "docker.io/demo/hand:1", "org.opencontainers.image.ref.name": "1"})

    def test_same_input_same_image_id_and_epoch_changes_it(self):
        self.assertEqual(self.build(arch="amd64").image_id, self.build(arch="amd64").image_id)
        self.assertNotEqual(self.build(arch="amd64").image_id, self.build(arch="amd64", epoch=1700000000).image_id)
        self.assertNotEqual(self.build(arch="amd64").image_id, self.build(arch="arm64").image_id)

    def test_bad_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            self.build(tag="Demo:1")
        with self.assertRaises(ValueError):
            self.build(tag="demo")
        with self.assertRaises(ValueError):
            self.build(sources=[])
        with self.assertRaises(ValueError):
            self.build(sources=[os.path.join(self.root, "missing")])
        with self.assertRaises(ValueError):
            main.parse_pairs(["novalue"], "--env")


class ArchiveTests(Fixture):
    def test_archive_carries_both_indexes_and_verifies(self):
        image = self.build(arch="amd64")
        out = os.path.join(self.root, "img.tar")
        size = main.write_archive(image, out)
        self.assertEqual(size, os.path.getsize(out))
        with tarfile.open(out) as tar:
            names = tar.getnames()
            legacy = json.loads(tar.extractfile("manifest.json").read())[0]
            modes = {m.name: (m.mtime, m.uid) for m in tar.getmembers()}
        for name in ("oci-layout", "index.json", "manifest.json", "blobs", "blobs/sha256"):
            self.assertIn(name, names)
        self.assertEqual(legacy["RepoTags"], ["demo/hand:1"])
        self.assertEqual(legacy["Config"], main.blob_path(image.image_id))
        self.assertEqual(legacy["Layers"], [main.blob_path(layer.digest) for layer in image.layers])
        self.assertTrue(all(name in names for name in [legacy["Config"], *legacy["Layers"]]))
        self.assertTrue(all(value == (0, 0) for value in modes.values()), "archive metadata is normalised too")
        info = main.verify_archive(out)
        self.assertEqual((info["tag"], info["image_id"], info["manifest_digest"]), ("demo/hand:1", image.image_id, image.manifest_digest))
        self.assertEqual(len(info["layers"]), 2)
        self.assertEqual(info["chain_ids"], main.chain_ids([layer.diff_id for layer in image.layers]))
        self.assertEqual(main.write_archive(image, os.path.join(self.root, "img2.tar")), size)
        with open(out, "rb") as a, open(os.path.join(self.root, "img2.tar"), "rb") as b:
            self.assertEqual(a.read(), b.read(), "two archives from the same image are byte-identical")

    def test_tampered_layer_is_rejected_by_verify(self):
        image = self.build(arch="amd64")
        out = os.path.join(self.root, "img.tar")
        main.write_archive(image, out)
        evil = os.path.join(self.root, "evil.tar")
        with tarfile.open(out) as src, tarfile.open(evil, "w") as dst:
            for member in src.getmembers():
                data = src.extractfile(member).read() if member.isfile() else None
                if member.name == main.blob_path(image.layers[1].digest):
                    data = main.gzip_bytes(main.layer_tar(self.base))
                    member.size = len(data)
                dst.addfile(member, io.BytesIO(data) if data is not None else None)
        with self.assertRaises(ValueError):
            main.verify_archive(evil)

    def test_oci_dir_is_the_same_blobs_on_disk(self):
        image = self.build(arch="amd64")
        oci = os.path.join(self.root, "oci")
        written = main.write_oci_dir(image, oci)
        self.assertIn("oci-layout", written)
        self.assertNotIn("manifest.json", written)
        with open(os.path.join(oci, "oci-layout")) as handle:
            self.assertEqual(json.load(handle)["imageLayoutVersion"], "1.0.0")
        with open(os.path.join(oci, "index.json")) as handle:
            index = json.load(handle)
        manifest_file = os.path.join(oci, main.blob_path(index["manifests"][0]["digest"]))
        self.assertTrue(os.path.isfile(manifest_file))
        for layer in image.layers:
            with open(os.path.join(oci, main.blob_path(layer.digest)), "rb") as handle:
                self.assertEqual(main.sha256_digest(handle.read()), layer.digest)

    def test_cli_build_then_verify(self):
        out = os.path.join(self.root, "cli.tar")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main.main(["build", "--layer", self.base, "--layer", self.app, "--tag", "demo/cli:1", "--entrypoint", "/bin/sh", "--cmd=-c", "--cmd", "cat /app/hello.txt", "--arch", "amd64", "--label", "a=b", "--out", out, "--json"])
        self.assertEqual(code, 0)
        info = json.loads(stdout.getvalue())
        self.assertEqual(info["config"]["config"]["Cmd"], ["-c", "cat /app/hello.txt"])
        self.assertEqual(info["archive"]["size"], os.path.getsize(out))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main.main(["verify", out]), 0)
        self.assertIn("every digest verified", stdout.getvalue())
        self.assertIn(info["image_id"], stdout.getvalue())
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(main.main(["build", "--layer", self.base, "--tag", "NoTag", "--out", out]), 1)
        self.assertIn("name:tag", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
