import io
import json
import os
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


def tar_bytes(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, content in files.items():
            if name.endswith("/"):
                info = tarfile.TarInfo(name.rstrip("/"))
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
                continue
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class FakeRegistry:
    """Enough of the distribution API for one image: token challenge, index, manifest, blobs."""

    def __init__(self):
        self.layer1 = tar_bytes({"etc/": b"", "etc/os-release": b"ID=demo\n", "app/secret": b"s3cret"})
        self.layer2 = tar_bytes({"app/.wh.secret": b"", "app/main.py": b"print('hi')\n"})
        self.config = json.dumps({"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers"}}).encode()
        self.blobs = {main.sha256_digest(b): b for b in (self.layer1, self.layer2, self.config)}
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": main.sha256_digest(self.config), "size": len(self.config)},
            "layers": [
                {"mediaType": "application/vnd.oci.image.layer.v1.tar", "digest": main.sha256_digest(self.layer1), "size": len(self.layer1)},
                {"mediaType": "application/vnd.oci.image.layer.v1.tar", "digest": main.sha256_digest(self.layer2), "size": len(self.layer2)},
            ],
        }
        self.manifest_bytes = json.dumps(manifest).encode()
        self.manifest_digest = main.sha256_digest(self.manifest_bytes)
        arm_manifest = dict(manifest, annotations={"org.opencontainers.image.ref.name": "arm64"})
        self.arm_bytes = json.dumps(arm_manifest).encode()
        self.arm_digest = main.sha256_digest(self.arm_bytes)
        index = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": self.arm_digest, "size": len(self.arm_bytes), "platform": {"os": "linux", "architecture": "arm64"}},
                {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": self.manifest_digest, "size": len(self.manifest_bytes), "platform": {"os": "linux", "architecture": "amd64"}},
            ],
        }
        self.index_bytes = json.dumps(index).encode()
        self.requests = []
        self.tampered = False

    def __call__(self, url, headers):
        self.requests.append((url, dict(headers)))
        if url.startswith("https://auth.example/token"):
            return 200, {}, json.dumps({"token": "tok-123"}).encode()
        if headers.get("Authorization") != "Bearer tok-123":
            return 401, {"www-authenticate": 'Bearer realm="https://auth.example/token",service="registry.example",scope="repository:demo/app:pull"'}, b"{}"
        if url.endswith("/manifests/1.0"):
            return 200, {"content-type": "application/vnd.oci.image.index.v1+json", "docker-content-digest": main.sha256_digest(self.index_bytes)}, self.index_bytes
        if url.endswith(f"/manifests/{self.manifest_digest}"):
            return 200, {"content-type": "application/vnd.oci.image.manifest.v1+json", "docker-content-digest": self.manifest_digest}, self.manifest_bytes
        if url.endswith(f"/manifests/{self.arm_digest}"):
            return 200, {"content-type": "application/vnd.oci.image.manifest.v1+json", "docker-content-digest": self.arm_digest}, self.arm_bytes
        if "/blobs/" in url:
            digest = url.rsplit("/", 1)[-1]
            body = self.blobs.get(digest)
            if body is None:
                return 404, {}, b"not found"
            if self.tampered and digest == main.sha256_digest(self.layer2):
                return 200, {}, body + b"x"
            return 200, {}, body
        return 404, {}, b"not found"


class ReferenceTests(unittest.TestCase):
    def test_parse_reference_defaults_and_registries(self):
        self.assertEqual(main.parse_reference("alpine"), main.Reference("registry-1.docker.io", "library/alpine", "latest", None))
        self.assertEqual(main.parse_reference("alpine:3.22").tag, "3.22")
        self.assertEqual(main.parse_reference("docker.io/nginx:1.27").repository, "library/nginx")
        self.assertEqual(main.parse_reference("ghcr.io/org/app:v2"), main.Reference("ghcr.io", "org/app", "v2", None))
        self.assertEqual(main.parse_reference("localhost:5000/app").registry, "localhost:5000")
        ref = main.parse_reference("ghcr.io/org/app@sha256:" + "b" * 64)
        self.assertEqual((ref.tag, ref.digest), (None, "sha256:" + "b" * 64))
        self.assertEqual(str(main.parse_reference("alpine:3.22")), "registry-1.docker.io/library/alpine:3.22")

    def test_parse_challenge_builds_the_token_url(self):
        url = main.parse_challenge('Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:library/alpine:pull"')
        self.assertEqual(url, "https://auth.docker.io/token?service=registry.docker.io&scope=repository%3Alibrary%2Falpine%3Apull")
        self.assertIsNone(main.parse_challenge('Basic realm="x"'))

    def test_select_platform_and_digest_helpers(self):
        index = {"manifests": [{"digest": "sha256:a", "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}}, {"digest": "sha256:b", "platform": {"os": "linux", "architecture": "amd64"}}]}
        self.assertEqual(main.select_platform(index, "linux", "arm64")["digest"], "sha256:a")
        self.assertEqual(main.select_platform(index)["digest"], "sha256:b")
        with self.assertRaises(LookupError):
            main.select_platform(index, "windows", "amd64")
        main.verify_digest(b"abc", main.sha256_digest(b"abc"))
        with self.assertRaises(ValueError):
            main.verify_digest(b"abc", "sha256:" + "0" * 64)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.registry = FakeRegistry()
        self.client = main.Client(self.registry)
        self.ref = main.Reference("registry.example", "demo/app", "1.0", None)

    def test_manifest_fetch_handles_token_challenge_and_platform_selection(self):
        manifest, digest = self.client.fetch_manifest(self.ref)
        self.assertEqual(digest, self.registry.manifest_digest)
        self.assertEqual(len(manifest["layers"]), 2)
        urls = [u for u, _ in self.registry.requests]
        self.assertTrue(any(u.startswith("https://auth.example/token?") for u in urls), "token endpoint must be called after the 401")
        self.assertEqual(urls[0], "https://registry.example/v2/demo/app/manifests/1.0")
        self.assertIn("application/vnd.oci.image.index.v1+json", self.registry.requests[0][1]["Accept"])
        arm_manifest, arm_digest = self.client.fetch_manifest(self.ref, "linux", "arm64")
        self.assertEqual(arm_digest, self.registry.arm_digest)
        self.assertNotEqual(arm_digest, digest)
        with self.assertRaises(LookupError):
            self.client.fetch_manifest(self.ref, "windows", "amd64")

    def test_pull_writes_verified_blobs_and_manifest(self):
        with tempfile.TemporaryDirectory() as dest:
            result = self.client.pull(self.ref, dest)
            self.assertEqual(len(result["layers"]), 2)
            self.assertTrue(os.path.isfile(os.path.join(dest, "manifest.json")))
            for layer in result["layers"]:
                self.assertTrue(os.path.isfile(layer["path"]))
                with open(layer["path"], "rb") as handle:
                    main.verify_digest(handle.read(), layer["digest"])

    def test_tampered_blob_is_rejected(self):
        self.registry.tampered = True
        with tempfile.TemporaryDirectory() as dest:
            with self.assertRaises(ValueError):
                self.client.pull(self.ref, dest)

    def test_rootfs_extraction_applies_whiteouts(self):
        with tempfile.TemporaryDirectory() as dest:
            result = self.client.pull(self.ref, dest)
            rootfs = os.path.join(dest, "rootfs")
            added, removed = main.extract_rootfs([layer["path"] for layer in result["layers"]], rootfs)
            self.assertTrue(os.path.isfile(os.path.join(rootfs, "etc", "os-release")))
            self.assertTrue(os.path.isfile(os.path.join(rootfs, "app", "main.py")))
            self.assertFalse(os.path.exists(os.path.join(rootfs, "app", "secret")), "whiteout must delete the lower file")
            self.assertEqual(removed, 1)
            self.assertGreaterEqual(added, 3)

    def test_layer_entries_cannot_escape_the_rootfs(self):
        evil = tar_bytes({"../../etc/passwd": b"root::0:0"})
        with tempfile.TemporaryDirectory() as rootfs:
            with self.assertRaises(ValueError):
                main.apply_layer(evil, rootfs)


if __name__ == "__main__":
    unittest.main()
