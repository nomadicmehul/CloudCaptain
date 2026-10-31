import contextlib
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
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def fake_image(path):
    layer_one = tar_bytes({"app/secret.txt": b"s" * 100, "app/main.py": b"m" * 200})
    layer_two = tar_bytes({"app/.wh.secret.txt": b"", "app/main.py": b"M" * 300, "lib/x.so": b"x" * 1000})
    config = {
        "history": [
            {"created_by": "/bin/sh -c #(nop)  COPY app /app"},
            {"created_by": "/bin/sh -c #(nop)  CMD [\"python\"]", "empty_layer": True},
            {"created_by": "/bin/sh -c rm /app/secret.txt && apt-get install lib"},
        ]
    }
    manifest = [{"Config": "config.json", "RepoTags": ["demo:latest"], "Layers": ["one/layer.tar", "blobs/sha256/two"]}]
    with tarfile.open(path, mode="w") as tar:
        for name, data in {
            "manifest.json": json.dumps(manifest).encode(),
            "config.json": json.dumps(config).encode(),
            "one/layer.tar": layer_one,
            "blobs/sha256/two": layer_two,
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


class HelperTests(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(main.human_size(0), "0 B")
        self.assertEqual(main.human_size(1536), "1.5 KB")
        self.assertEqual(main.human_size(5 * 1024 ** 2), "5.0 MB")
        self.assertEqual(main.human_size(3 * 1024 ** 3), "3.0 GB")

    def test_history_instructions_skips_empty_layers_and_cleans_prefixes(self):
        config = {
            "history": [
                {"created_by": "/bin/sh -c #(nop)  COPY app /app"},
                {"created_by": "/bin/sh -c #(nop)  CMD [\"python\"]", "empty_layer": True},
                {"created_by": "/bin/sh -c apt-get   install lib"},
            ]
        }
        self.assertEqual(main.history_instructions(config), ["COPY app /app", "RUN apt-get install lib"])

    def test_parse_layer_collects_files_and_whiteouts(self):
        data = tar_bytes({"./app/.wh.secret.txt": b"", "app/main.py": b"abc", "etc/.wh..wh..opq": b""})
        layer = main.parse_layer("l", data)
        self.assertEqual(layer.files, {"app/main.py": 3})
        self.assertEqual(layer.size, 3)
        self.assertEqual(layer.whiteouts, ["app/secret.txt", "etc/*"])


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.image = fake_image(os.path.join(self.tmp.name, "demo.tar"))
        self.report = main.load_image(self.image)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_image_aligns_instructions_with_layers(self):
        self.assertEqual(self.report.tags, ["demo:latest"])
        self.assertEqual([layer.created_by for layer in self.report.layers], ["COPY app /app", "RUN rm /app/secret.txt && apt-get install lib"])
        self.assertEqual(self.report.total, 100 + 200 + 300 + 1000)

    def test_deleted_files_reports_secret_removed_in_later_layer(self):
        self.assertEqual(main.deleted_files(self.report.layers), [("app/secret.txt", 100, 1)])

    def test_shadowed_files_reports_overwritten_main(self):
        self.assertEqual(main.shadowed_files(self.report.layers), [("app/main.py", 200, 1)])

    def test_wasted_bytes_counts_deleted_and_overwritten(self):
        self.assertEqual(main.wasted_bytes(self.report.layers), 300)

    def test_main_renders_summary_and_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main.main([self.image]), 0)
        text = out.getvalue()
        self.assertIn("layers: 2", text)
        self.assertIn("wasted: 300 B", text)
        self.assertIn("deleted in layer 1: app/secret.txt", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main.main([self.image, "--json"])
        data = json.loads(out.getvalue())
        self.assertEqual(data["wasted"], 300)
        self.assertEqual(data["layers"][1]["whiteouts"], ["app/secret.txt"])


if __name__ == "__main__":
    unittest.main()
