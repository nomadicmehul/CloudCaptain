import contextlib
import hashlib
import http.server
import io
import json
import os
import pickle
import re
import struct
import sys
import tempfile
import threading
import unittest
import uuid
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

U8, I8, U16, I16, U32, I32, F32, BOOL, STRING, ARRAY, U64, I64, F64 = 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12
T_F32, T_F16, T_Q4_0, T_Q8_0, T_Q4_K, T_BF16 = 0, 1, 2, 8, 12, 30
SCALAR_FORMATS = {U8: "<B", I8: "<b", U16: "<H", I16: "<h", U32: "<I", I32: "<i", F32: "<f", BOOL: "<?", U64: "<Q", I64: "<q", F64: "<d"}
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


def gguf_str(text):
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def gguf_value(value_type, value):
    if value_type in SCALAR_FORMATS:
        return struct.pack(SCALAR_FORMATS[value_type], value)
    if value_type == STRING:
        return gguf_str(value)
    item_type, items = value
    return struct.pack("<IQ", item_type, len(items)) + b"".join(gguf_value(item_type, item) for item in items)


def pad(data, alignment):
    return data + b"\0" * (-len(data) % alignment)


def make_gguf(kvs, tensors, alignment=32, version=3, offsets=None):
    out = b"GGUF" + struct.pack("<IQQ", version, len(tensors), len(kvs))
    for key, value_type, value in kvs:
        out += gguf_str(key) + struct.pack("<I", value_type) + gguf_value(value_type, value)
    data = b""
    for index, (name, dims, type_id, payload) in enumerate(tensors):
        data = pad(data, alignment)
        offset = len(data) if offsets is None else offsets[index]
        out += gguf_str(name) + struct.pack("<I", len(dims)) + b"".join(struct.pack("<Q", d) for d in dims) + struct.pack("<IQ", type_id, offset)
        data += payload
    return pad(out, alignment) + data


def tiny_llama_kvs(extra=()):
    return [
        ("general.architecture", STRING, "llama"),
        ("general.name", STRING, "tiny"),
        ("general.basename", STRING, "tinyllama"),
        ("general.license", STRING, "apache-2.0"),
        ("general.file_type", U32, 7),
        ("llama.block_count", U32, 2),
        ("llama.context_length", U32, 128),
        ("llama.embedding_length", U32, 64),
        ("llama.attention.head_count", U32, 4),
        ("llama.attention.head_count_kv", U32, 2),
        ("llama.rope.freq_base", F32, 10000.0),
        ("tokenizer.ggml.add_bos_token", BOOL, True),
        ("tokenizer.ggml.tokens", ARRAY, (STRING, ["<s>", "</s>", "hi"])),
        ("tokenizer.ggml.token_type", ARRAY, (I32, [3, 3, 1])),
    ] + list(extra)


EMBED_VALUES = [((i % 32) - 16) / 8.0 for i in range(64 * 3)]


def tiny_llama_tensors():
    embed = b"".join(main.quantize_q8_0(EMBED_VALUES[i:i + 32]) for i in range(0, len(EMBED_VALUES), 32))
    return [
        ("token_embd.weight", [64, 3], T_Q8_0, embed),
        ("blk.0.attn_norm.weight", [64], T_F32, struct.pack("<64f", *([1.0] * 64))),
        ("blk.0.ffn_up.weight", [256, 2], T_Q4_K, b"\x11" * 288),
        ("blk.0.attn_q.weight", [64, 64], T_F16, struct.pack("<4096e", *([0.5] * 4096))),
    ]


def make_safetensors(tensors, metadata=None, header_pad=8):
    header = {}
    offset = 0
    for name, dtype, shape, payload in tensors:
        header[name] = {"dtype": dtype, "shape": shape, "data_offsets": [offset, offset + len(payload)]}
        offset += len(payload)
    if metadata is not None:
        header["__metadata__"] = metadata
    raw = json.dumps(header).encode("utf-8")
    raw += b" " * (-len(raw) % header_pad)
    return struct.pack("<Q", len(raw)) + raw + b"".join(t[3] for t in tensors)


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path


@contextlib.contextmanager
def serve(handler_class):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


class QuietHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body=b"", headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def range_handler(payload, honour_range=True):
    class Handler(QuietHandler):
        requests = []

        def do_GET(self):
            match = re.match(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
            Handler.requests.append(self.headers.get("Range"))
            if not honour_range or not match:
                return self.reply(200, payload)
            start, end = int(match.group(1)), min(int(match.group(2)), len(payload) - 1)
            if start >= len(payload):
                return self.reply(416, b"", {"Content-Range": f"bytes */{len(payload)}"})
            self.reply(206, payload[start:end + 1], {"Content-Range": f"bytes {start}-{end}/{len(payload)}"})

    return Handler


def hub_handler(files, commits, corrupt=False):
    class Handler(QuietHandler):
        cdn_gets = []

        def route(self):
            match = re.match(r"^/(.+?/.+?)/resolve/([^/]+)/(.+)$", self.path)
            if self.path.startswith("/cdn/"):
                key = self.path[5:]
                data = next(v for v in files.values() if hashlib.sha256(v).hexdigest() == key)
                if self.command == "GET":
                    Handler.cdn_gets.append(key)
                if corrupt:
                    data = data[:-1] + bytes([data[-1] ^ 1])
                return self.reply(200, data)
            if self.path.startswith("/cache/"):
                return self.reply(200, files[self.path[7:]])
            if not match:
                return self.reply(404, b"no route")
            repo, revision, name = match.groups()
            commit = commits.get(revision)
            if commit is None:
                return self.reply(404, b"", {"X-Error-Code": "RevisionNotFound"})
            if name not in files:
                return self.reply(404, b"", {"X-Error-Code": "EntryNotFound"})
            data = files[name]
            if name.endswith(".gguf") or name.endswith(".safetensors"):
                sha = hashlib.sha256(data).hexdigest()
                return self.reply(302, b"", {"Location": f"/cdn/{sha}", "X-Repo-Commit": commit, "X-Linked-Etag": f'"{sha}"', "X-Linked-Size": str(len(data))})
            return self.reply(307, b"", {"Location": f"/cache/{name}", "X-Repo-Commit": commit, "X-Linked-Etag": f'"{main.git_blob_sha1(data)}"'})

        do_GET = route
        do_HEAD = route

    return Handler


def registry_handler(tamper=False):
    class Handler(QuietHandler):
        blobs = {}
        manifests = {}
        uploads = {}
        calls = []

        def body(self):
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def route(self):
            Handler.calls.append(f"{self.command} {self.path.split('?')[0]}")
            path, _, query = self.path.partition("?")
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            if path == "/v2/":
                return self.reply(200, b"{}")
            upload = re.match(r"^/v2/(.+)/blobs/uploads/(.*)$", path)
            if upload:
                repo, session = upload.groups()
                if self.command == "POST":
                    session = uuid.uuid4().hex
                    Handler.uploads[session] = repo
                    return self.reply(202, b"", {"Location": f"/v2/{repo}/blobs/uploads/{session}?_state=abc", "Docker-Upload-UUID": session})
                if self.command == "PUT":
                    data = self.body()
                    digest = params.get("digest", "").replace("%3A", ":")
                    if session not in Handler.uploads or "sha256:" + hashlib.sha256(data).hexdigest() != digest:
                        return self.reply(400, b'{"errors":[{"code":"DIGEST_INVALID"}]}')
                    Handler.blobs[(repo, digest)] = data
                    return self.reply(201, b"", {"Docker-Content-Digest": digest, "Location": f"/v2/{repo}/blobs/{digest}"})
            blob = re.match(r"^/v2/(.+)/blobs/(sha256:[0-9a-f]{64})$", path)
            if blob:
                key = blob.groups()
                if key not in Handler.blobs:
                    return self.reply(404, b'{"errors":[{"code":"BLOB_UNKNOWN"}]}')
                data = Handler.blobs[key]
                if tamper and len(data) > 200:
                    data = data[:-1] + bytes([data[-1] ^ 1])
                return self.reply(200, data, {"Docker-Content-Digest": key[1]})
            manifest = re.match(r"^/v2/(.+)/manifests/(.+)$", path)
            if manifest:
                repo, reference = manifest.groups()
                if self.command == "PUT":
                    data = self.body()
                    document = json.loads(data)
                    for descriptor in [document["config"]] + document["layers"]:
                        if (repo, descriptor["digest"]) not in Handler.blobs:
                            return self.reply(400, b'{"errors":[{"code":"MANIFEST_BLOB_UNKNOWN"}]}')
                    digest = "sha256:" + hashlib.sha256(data).hexdigest()
                    Handler.manifests[(repo, reference)] = data
                    Handler.manifests[(repo, digest)] = data
                    return self.reply(201, b"", {"Docker-Content-Digest": digest})
                data = Handler.manifests.get((repo, reference))
                if data is None:
                    return self.reply(404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}')
                return self.reply(200, data, {"Content-Type": json.loads(data)["mediaType"], "Docker-Content-Digest": "sha256:" + hashlib.sha256(data).hexdigest()})
            return self.reply(404, b"no route")

        do_GET = route
        do_HEAD = route
        do_POST = route
        do_PUT = route

    return Handler


class GgufTests(TempDirCase):
    def test_header_metadata_and_tensors_parse_with_types_and_an_aligned_data_offset(self):
        path = self.write("tiny.gguf", make_gguf(tiny_llama_kvs(), tiny_llama_tensors()))
        model, _ = main.load_model(path)
        self.assertEqual((model.format, model.version, model.alignment), ("gguf", 3, 32))
        self.assertEqual(model.metadata["general.architecture"], "llama")
        self.assertEqual(model.metadata["tokenizer.ggml.tokens"], ["<s>", "</s>", "hi"])
        self.assertEqual(model.metadata["tokenizer.ggml.token_type"], [3, 3, 1])
        self.assertIs(model.metadata["tokenizer.ggml.add_bos_token"], True)
        self.assertEqual(model.value_types["llama.rope.freq_base"], "FLOAT32")
        self.assertEqual(model.data_offset % 32, 0)
        self.assertEqual([t.name for t in model.tensors], ["token_embd.weight", "blk.0.attn_norm.weight", "blk.0.ffn_up.weight", "blk.0.attn_q.weight"])
        self.assertTrue(all(t.offset % 32 == 0 for t in model.tensors))
        self.assertEqual(main.file_type_label(model), "MOSTLY_Q8_0")

    def test_parameter_and_byte_counts_follow_each_block_format(self):
        model, _ = main.load_model(self.write("tiny.gguf", make_gguf(tiny_llama_kvs(), tiny_llama_tensors())))
        table = model.by_dtype()
        self.assertEqual(table["Q8_0"], {"tensors": 1, "parameters": 192, "bytes": 6 * 34})
        self.assertEqual(table["F32"], {"tensors": 1, "parameters": 64, "bytes": 256})
        self.assertEqual(table["Q4_K"], {"tensors": 1, "parameters": 512, "bytes": 2 * 144})
        self.assertEqual(table["F16"], {"tensors": 1, "parameters": 4096, "bytes": 8192})
        self.assertEqual(model.parameters, 192 + 64 + 512 + 4096)
        self.assertEqual(model.tensor_bytes, 204 + 256 + 288 + 8192)

    def test_shape_comes_from_the_architecture_prefixed_keys(self):
        model, _ = main.load_model(self.write("tiny.gguf", make_gguf(tiny_llama_kvs(), tiny_llama_tensors())))
        shape = main.gguf_shape(model)
        self.assertEqual((shape.architecture, shape.layers, shape.heads, shape.kv_heads, shape.key_dim, shape.context_length, shape.vocab), ("llama", 2, 4, [2, 2], 16, 128, 3))
        self.assertEqual(main.kv_cache_bytes(shape, 1), 2 * 2 * (16 + 16) * 2)

    def test_custom_alignment_is_honoured_and_misaligned_offsets_are_rejected(self):
        tensors = [("a", [64], T_F32, b"\0" * 256), ("b", [64], T_F32, b"\0" * 256)]
        model, _ = main.load_model(self.write("aligned.gguf", make_gguf([("general.alignment", U32, 64)], tensors, alignment=64)))
        self.assertEqual(model.alignment, 64)
        self.assertEqual(model.data_offset % 64, 0)
        with self.assertRaisesRegex(main.InputError, "not aligned to 64"):
            main.load_model(self.write("misaligned.gguf", make_gguf([("general.alignment", U32, 64)], tensors, alignment=64, offsets=[0, 32])))
        with self.assertRaisesRegex(main.InputError, "power of two"):
            main.load_model(self.write("odd.gguf", make_gguf([("general.alignment", U32, 48)], tensors)))

    def test_malformed_files_fail_with_a_reason_instead_of_a_crash(self):
        good = make_gguf(tiny_llama_kvs(), tiny_llama_tensors())
        cases = {
            "neither GGUF nor safetensors": b"GGUX" + good[4:],
            "obsolete": b"GGUF" + struct.pack("<I", 1) + good[8:],
            "big-endian": b"GGUF" + struct.pack(">I", 3) + good[8:],
            "at most 4": make_gguf([], [("t", [1, 1, 1, 1, 1], T_F32, b"\0" * 4)]),
            "unknown ggml type": make_gguf([], [("t", [32], 99, b"\0" * 128)]),
            "past the end": make_gguf([], [("t", [64], T_F32, b"\0" * 128)]),
            "overlaps": make_gguf([], [("a", [64], T_F32, b"\0" * 256), ("b", [64], T_F32, b"\0" * 256)], offsets=[0, 128]),
            "multiple of the Q8_0 block": make_gguf([], [("t", [48], T_Q8_0, b"\0" * 64)]),
            "duplicate metadata": make_gguf([("k", U8, 1), ("k", U8, 2)], []),
            "duplicate tensor": make_gguf([], [("t", [32], T_F32, b"\0" * 128), ("t", [32], T_F32, b"\0" * 128)]),
            "remain": b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", 10**12) + b"key" + b"\0" * 64,
            "cannot fit": b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + gguf_str("k") + struct.pack("<IIQ", ARRAY, U32, 2**40),
        }
        for reason, data in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(main.InputError, reason):
                    main.load_model(self.write("bad.gguf", data))

    def test_peek_dequantizes_q8_0_and_decodes_f16_and_f32(self):
        path = self.write("tiny.gguf", make_gguf(tiny_llama_kvs(), tiny_llama_tensors()))
        model, source = main.load_model(path)
        values = main.peek(model, source, "token_embd.weight", 40)
        self.assertEqual(len(values), 40)
        for got, want in zip(values, EMBED_VALUES[:40]):
            self.assertAlmostEqual(got, want, delta=2 / 127)
        self.assertEqual(main.peek(model, source, "blk.0.attn_q.weight", 3), [0.5, 0.5, 0.5])
        self.assertEqual(main.peek(model, source, "blk.0.attn_norm.weight", 2), [1.0, 1.0])
        with self.assertRaisesRegex(main.InputError, "K-quant"):
            main.peek(model, source, "blk.0.ffn_up.weight", 2)
        with self.assertRaisesRegex(main.InputError, "no tensor named"):
            main.peek(model, source, "missing", 2)


class SafetensorsTests(TempDirCase):
    def tensors(self):
        return [
            ("model.embed.weight", "BF16", [8, 4], b"\x80\x3f" * 32),
            ("model.norm.weight", "F32", [4], struct.pack("<4f", 1, 2, 3, 4)),
            ("model.mask", "BOOL", [3], b"\x01\x00\x01"),
        ]

    def test_header_gives_dtypes_shapes_metadata_and_offsets_without_reading_weights(self):
        path = self.write("m.safetensors", make_safetensors(self.tensors(), {"format": "pt"}))
        model, source = main.load_model(path)
        self.assertEqual(model.format, "safetensors")
        self.assertEqual(model.metadata, {"format": "pt"})
        self.assertEqual(model.parameters, 32 + 4 + 3)
        self.assertEqual(model.by_dtype()["BF16"], {"tensors": 1, "parameters": 32, "bytes": 64})
        self.assertEqual(model.tensor("model.norm.weight").offset, model.data_offset + 64)
        self.assertEqual(main.peek(model, source, "model.embed.weight", 2), [1.0, 1.0])
        self.assertEqual(main.peek(model, source, "model.norm.weight", 4), [1.0, 2.0, 3.0, 4.0])

    def test_validation_follows_the_reference_implementation(self):
        good = make_safetensors(self.tensors())
        header_len = struct.unpack("<Q", good[:8])[0]
        gap = json.dumps({"a": {"dtype": "F32", "shape": [1], "data_offsets": [4, 8]}}).encode()
        wrong = json.dumps({"a": {"dtype": "F32", "shape": [2], "data_offsets": [0, 4]}}).encode()
        cases = {
            "refuses headers above": struct.pack("<Q", 200_000_000) + b"{",
            "contiguous": struct.pack("<Q", len(gap)) + gap + b"\0" * 8,
            "needs 8": struct.pack("<Q", len(wrong)) + wrong + b"\0" * 4,
            "account for": good + b"extra",
            "unknown dtype": make_safetensors([("a", "F12", [1], b"\0\0")]),
            "strings to strings": make_safetensors([("a", "U8", [1], b"\0")], {"n": 1}),
            "neither GGUF nor safetensors": struct.pack("<Q", header_len) + b"[" + good[9:],
            "whole number of bytes": make_safetensors([("a", "F4", [3], b"\0\0")]),
        }
        for reason, data in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(main.InputError, reason):
                    main.load_model(self.write("bad.safetensors", data))

    def test_a_config_json_next_to_the_file_supplies_layers_and_heads(self):
        path = self.write("repo/model.safetensors", make_safetensors(self.tensors()))
        self.write("repo/config.json", json.dumps({"model_type": "llama", "num_hidden_layers": 30, "num_attention_heads": 9, "num_key_value_heads": 3, "hidden_size": 576, "max_position_embeddings": 8192, "vocab_size": 49152}))
        model, _ = main.load_model(path)
        shape = main.model_shape(model, None)
        self.assertEqual((shape.layers, shape.kv_heads[0], shape.key_dim, shape.vocab), (30, 3, 64, 49152))

    def test_pickle_checkpoints_are_refused_and_never_unpickled(self):
        marker = os.path.join(self.dir, "pwned")

        class Payload:
            def __reduce__(self):
                return (open, (marker, "w"))

        raw = pickle.dumps(Payload(), protocol=2)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("archive/data.pkl", raw)
        for name, data in (("pytorch_model.bin", buffer.getvalue()), ("raw.pkl", raw)):
            with self.subTest(name=name):
                code, _, err = run(["inspect", self.write(name, data)])
                self.assertEqual(code, 2)
                self.assertIn("pickle", err)
                self.assertFalse(os.path.exists(marker))


class QuantizationTests(unittest.TestCase):
    def test_bits_per_weight_and_weight_bytes_per_format(self):
        self.assertEqual(main.bits_per_weight("Q8_0"), 8.5)
        self.assertEqual(main.bits_per_weight("Q4_0"), 4.5)
        self.assertEqual(main.bits_per_weight("Q4_K"), 4.5)
        self.assertEqual(main.bits_per_weight("Q6_K"), 6.5625)
        self.assertEqual(main.bits_per_weight("Q2_K"), 2.625)
        self.assertEqual(main.bits_per_weight("BF16"), 16)
        self.assertEqual(main.weight_bytes(8 * 10**9, "BF16"), 16 * 10**9)
        self.assertEqual(main.weight_bytes(8 * 10**9, "q4_k"), 4_500_000_000)
        self.assertEqual(main.weight_bytes(33, "Q8_0"), 2 * 34)
        self.assertEqual(main.weight_bytes(3, "F4"), 2)
        with self.assertRaises(main.InputError):
            main.weight_bytes(10, "Q3_X")

    def test_q8_0_is_34_bytes_and_round_trips_within_half_a_step(self):
        values = [((i * 37) % 64 - 32) / 10.0 for i in range(32)]
        block = main.quantize_q8_0(values)
        self.assertEqual(len(block), 34)
        scale = main.fp16_to_float(struct.unpack("<H", block[:2])[0])
        self.assertAlmostEqual(scale, max(abs(v) for v in values) / 127, places=4)
        for got, want in zip(main.dequantize_q8_0(block), values):
            self.assertLessEqual(abs(got - want), scale / 2 + 1e-3)
        self.assertEqual(main.dequantize_q8_0(main.quantize_q8_0([0.0] * 32)), [0.0] * 32)

    def test_q4_0_packs_two_values_per_byte_low_nibbles_first(self):
        values = [float(i - 8) for i in range(16)] + [float(7 - i) for i in range(16)]
        block = main.quantize_q4_0(values)
        self.assertEqual(len(block), 18)
        self.assertEqual(main.fp16_to_float(struct.unpack("<H", block[:2])[0]), 1.0)
        self.assertEqual(block[2] & 0x0F, 0)
        self.assertEqual(block[2] >> 4, 15)
        decoded = main.dequantize_q4_0(block)
        for got, want in zip(decoded, values):
            self.assertLessEqual(abs(got - want), 0.5)
        self.assertEqual(decoded[:16], [float(i - 8) for i in range(16)])

    def test_fp16_and_bf16_bit_patterns(self):
        self.assertEqual(main.fp16_to_float(0x3C00), 1.0)
        self.assertEqual(main.fp16_to_float(0xC000), -2.0)
        self.assertEqual(main.bf16_to_float(0x3F80), 1.0)
        self.assertEqual(main.bf16_to_float(0x4049), 3.140625)
        self.assertEqual(main.float_to_fp16_bits(0.5), 0x3800)


class MemoryTests(unittest.TestCase):
    LLAMA_3_8B = {"model_type": "llama", "num_hidden_layers": 32, "num_attention_heads": 32, "num_key_value_heads": 8, "hidden_size": 4096, "max_position_embeddings": 131072, "vocab_size": 128256}
    SMOLLM2 = {"model_type": "llama", "num_hidden_layers": 30, "num_attention_heads": 9, "num_key_value_heads": 3, "hidden_size": 576, "max_position_embeddings": 8192, "vocab_size": 49152}

    def test_llama_3_8b_needs_128_kib_per_token_and_4_times_more_without_gqa(self):
        shape = main.config_shape(self.LLAMA_3_8B)
        self.assertEqual(main.kv_cache_bytes(shape, 1), 128 * 1024)
        self.assertEqual(main.kv_cache_bytes(shape, 8192), 1 << 30)
        self.assertEqual(main.kv_cache_bytes(shape, 32768 * 16), 64 << 30)
        mha = main.config_shape(dict(self.LLAMA_3_8B, num_key_value_heads=32))
        self.assertEqual(main.kv_cache_bytes(mha, 1), 4 * 128 * 1024)

    def test_smollm2_kv_cache_is_180_mib_at_8192_tokens_like_llama_cpp_reports(self):
        shape = main.config_shape(self.SMOLLM2)
        self.assertEqual(main.kv_cache_bytes(shape, 1), 23040)
        self.assertEqual(main.kv_cache_bytes(shape, 8192), 180 << 20)
        self.assertEqual(main.kv_cache_bytes(shape, 2048), 45 << 20)

    def test_quantized_kv_types_scale_by_block_size_and_need_divisible_head_dims(self):
        shape = main.config_shape(self.SMOLLM2)
        self.assertEqual(main.kv_cache_bytes(shape, 8192, "q8_0"), (180 << 20) // 2 * 34 // 32)
        self.assertEqual(main.kv_cache_bytes(shape, 8192, "q4_0"), (180 << 20) // 2 * 18 // 32)
        self.assertEqual(main.kv_cache_bytes(shape, 8192, "f32"), 360 << 20)
        odd = main.config_shape(dict(self.SMOLLM2, head_dim=80))
        with self.assertRaisesRegex(main.InputError, "not multiples"):
            main.kv_cache_bytes(odd, 1, "q8_0")
        with self.assertRaisesRegex(main.InputError, "unknown KV cache type"):
            main.kv_cache_bytes(shape, 1, "q4_k")

    def test_gpu_budget_counts_the_kv_tokens_left_after_the_weights(self):
        shape = main.config_shape(self.LLAMA_3_8B)
        report = main.memory_report(8 * 10**9, shape, ["Q4_K"], 8192, 1, "f16", selected="Q4_K", gpu_gib=24, utilization=0.9)
        budget = int(24 * (1 << 30) * 0.9) - 4_500_000_000
        self.assertEqual(report["gpu"]["kv_budget_bytes"], budget)
        self.assertEqual(report["gpu"]["max_tokens"], budget // (128 * 1024))
        self.assertEqual(report["gpu"]["max_sequences_at_context"], budget // (128 * 1024) // 8192)
        self.assertEqual(report["total_bytes"], 4_500_000_000 + (1 << 30))

    def test_param_size_labels_follow_the_modelpack_format(self):
        self.assertEqual(main.param_size_label(134_515_008), "134.5M")
        self.assertEqual(main.param_size_label(14_067_712), "14.1M")
        self.assertEqual(main.param_size_label(8_030_261_248), "8B")
        self.assertEqual(main.param_size_label(70_553_706_496), "70.6B")
        self.assertEqual(main.param_size_label(1_200_000_000_000), "1.2T")


class DigestTests(TempDirCase):
    def test_digest_cli_exit_codes_for_match_mismatch_and_bad_input(self):
        path = self.write("blob", b"weights")
        digest = "sha256:" + hashlib.sha256(b"weights").hexdigest()
        self.assertEqual(run(["digest", path, "--expect", digest])[0], 0)
        self.assertEqual(run(["digest", path, "--expect", digest[7:].upper()])[0], 0)
        code, out, _ = run(["digest", path, "--expect", "sha256:" + "0" * 64])
        self.assertEqual(code, 1)
        self.assertIn("MISMATCH", out)
        self.assertEqual(run(["digest", path, "--expect", "md5:abc"])[0], 2)
        self.assertEqual(run(["digest", os.path.join(self.dir, "missing")])[0], 2)

    def test_git_blob_id_matches_git_hash_object(self):
        self.assertEqual(main.git_blob_sha1(b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")
        self.assertEqual(main.git_blob_sha1(b""), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")


class RemoteTests(unittest.TestCase):
    def test_a_remote_safetensors_header_costs_one_small_range_request(self):
        payload = make_safetensors([("w", "F16", [512, 1024], b"\0" * (512 * 1024 * 2))], {"format": "pt"})
        with serve(range_handler(payload)) as (_, port):
            model, source = main.load_model(f"http://127.0.0.1:{port}/model.safetensors")
        self.assertEqual(model.parameters, 512 * 1024)
        self.assertEqual(model.file_size, len(payload))
        self.assertEqual(source.requests, 1)
        self.assertLessEqual(source.fetched, main.FIRST_READ)

    def test_a_remote_gguf_is_read_in_growing_chunks_until_the_tensor_infos_end(self):
        tokens = [f"token-{i:05d}" for i in range(20000)]
        payload = make_gguf(tiny_llama_kvs([("tokenizer.ggml.merges", ARRAY, (STRING, tokens))]), tiny_llama_tensors() + [("big", [1024, 1024], T_F16, b"\0" * (2 << 20))])
        with serve(range_handler(payload)) as (_, port):
            model, source = main.load_model(f"http://127.0.0.1:{port}/m.gguf")
        self.assertEqual(len(model.metadata["tokenizer.ggml.merges"]), 20000)
        self.assertGreater(source.requests, 1)
        self.assertLess(source.fetched, len(payload))

    def test_a_server_that_ignores_range_is_refused(self):
        payload = make_safetensors([("w", "U8", [4], b"\0" * 4)])
        with serve(range_handler(payload, honour_range=False)) as (_, port):
            code, _, err = run(["inspect", f"http://127.0.0.1:{port}/m.safetensors"])
        self.assertEqual(code, 2)
        self.assertIn("Range", err)


class FetchTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.weights = make_gguf(tiny_llama_kvs(), tiny_llama_tensors())
        self.config = b'{"model_type": "llama"}\n'
        self.files = {"tiny.gguf": self.weights, "config.json": self.config}

    def test_fetch_by_commit_verifies_the_lfs_sha256_and_reports_the_commit(self):
        with serve(hub_handler(self.files, {COMMIT_A: COMMIT_A})) as (_, port):
            code, out, err = run(["fetch", "org/tiny", "tiny.gguf", "--revision", COMMIT_A, "--dest", self.dir, "--endpoint", f"http://127.0.0.1:{port}", "--json"])
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["commit"], COMMIT_A)
        self.assertEqual(result["digest"], "sha256:" + hashlib.sha256(self.weights).hexdigest())
        self.assertEqual(result["verified_against"], ["Hub LFS sha256 (x-linked-etag)"])
        with open(os.path.join(self.dir, "tiny.gguf"), "rb") as handle:
            self.assertEqual(handle.read(), self.weights)

    def test_a_pinned_digest_that_disagrees_with_the_hub_downloads_nothing(self):
        handler = hub_handler(self.files, {COMMIT_A: COMMIT_A})
        with serve(handler) as (_, port):
            code, _, err = run(["fetch", "org/tiny", "tiny.gguf", "--revision", COMMIT_A, "--sha256", "1" * 64, "--dest", self.dir, "--endpoint", f"http://127.0.0.1:{port}"])
        self.assertEqual(code, 1)
        self.assertIn("nothing was downloaded", err)
        self.assertEqual(handler.cdn_gets, [])
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_corrupted_download_fails_and_leaves_no_file(self):
        with serve(hub_handler(self.files, {COMMIT_A: COMMIT_A}, corrupt=True)) as (_, port):
            code, _, err = run(["fetch", "org/tiny", "tiny.gguf", "--revision", COMMIT_A, "--dest", self.dir, "--endpoint", f"http://127.0.0.1:{port}"])
        self.assertEqual(code, 1)
        self.assertIn("partial download was removed", err)
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_branch_revision_works_but_warns_with_the_commit_to_pin(self):
        with serve(hub_handler(self.files, {"main": COMMIT_B})) as (_, port):
            code, out, err = run(["fetch", "org/tiny", "tiny.gguf", "--revision", "main", "--dest", self.dir, "--endpoint", f"http://127.0.0.1:{port}"])
        self.assertEqual(code, 0, err)
        self.assertIn(f"pin --revision {COMMIT_B}", err)

    def test_small_files_are_verified_by_their_git_blob_id(self):
        with serve(hub_handler(self.files, {COMMIT_A: COMMIT_A})) as (_, port):
            result = main.fetch("org/tiny", "config.json", COMMIT_A, self.dir, endpoint=f"http://127.0.0.1:{port}")
        self.assertEqual(result["verified_against"], ["Hub git blob id (x-linked-etag)"])

    def test_unknown_revision_or_file_is_an_input_error(self):
        with serve(hub_handler(self.files, {COMMIT_A: COMMIT_A})) as (_, port):
            code, _, err = run(["fetch", "org/tiny", "tiny.gguf", "--revision", COMMIT_B, "--dest", self.dir, "--endpoint", f"http://127.0.0.1:{port}"])
            self.assertEqual(code, 2)
            self.assertIn("RevisionNotFound", err)
            code, _, err = run(["fetch", "org/tiny", "nope.gguf", "--revision", COMMIT_A, "--dest", self.dir, "--endpoint", f"http://127.0.0.1:{port}"])
            self.assertEqual(code, 2)
            self.assertIn("EntryNotFound", err)


class ArtifactTests(TempDirCase):
    def model_files(self):
        gguf = self.write("model/tiny-Q8_0.gguf", make_gguf(tiny_llama_kvs(), tiny_llama_tensors()))
        config = self.write("model/config.json", '{"model_type": "llama"}')
        readme = self.write("model/README.md", "# tiny\n")
        return gguf, config, readme

    def test_media_types_follow_the_modelpack_layer_kinds(self):
        self.assertEqual(main.classify("m.gguf"), (main.WEIGHT_RAW, False))
        self.assertEqual(main.classify("model-00001-of-00002.safetensors"), (main.WEIGHT_RAW, False))
        self.assertEqual(main.classify("tokenizer.json"), (main.WEIGHT_CONFIG_RAW, False))
        self.assertEqual(main.classify("model.safetensors.index.json"), (main.WEIGHT_CONFIG_RAW, False))
        self.assertEqual(main.classify("LICENSE"), (main.DOC_RAW, False))
        self.assertEqual(main.classify("handler.py"), (main.CODE_RAW, False))
        self.assertEqual(main.classify("eval_results.json"), (main.WEIGHT_CONFIG_RAW, True))
        self.assertEqual(main.split_file_arg("m.gguf:application/x-custom"), ("m.gguf", "application/x-custom"))
        self.assertEqual(main.split_file_arg("m.gguf"), ("m.gguf", None))

    def test_the_manifest_is_a_modelpack_artifact_with_one_layer_per_file_and_is_reproducible(self):
        gguf, config, readme = self.model_files()
        files = [(gguf, None), (config, None), (readme, None)]
        config_blob, layers, manifest = main.package(files, revision=COMMIT_A)
        document = json.loads(manifest)
        self.assertEqual(document["mediaType"], main.OCI_MANIFEST)
        self.assertEqual(document["artifactType"], main.MODEL_ARTIFACT)
        self.assertEqual(document["config"]["mediaType"], main.MODEL_CONFIG)
        self.assertEqual([layer["mediaType"] for layer in document["layers"]], [main.WEIGHT_RAW, main.WEIGHT_CONFIG_RAW, main.DOC_RAW])
        with open(gguf, "rb") as handle:
            self.assertEqual(document["layers"][0]["digest"], "sha256:" + hashlib.sha256(handle.read()).hexdigest())
        self.assertEqual(document["layers"][0]["annotations"], {main.TITLE: "tiny-Q8_0.gguf", main.FILEPATH: "tiny-Q8_0.gguf"})
        model_config = json.loads(config_blob.data)
        self.assertEqual(model_config["modelfs"], {"type": "layers", "diffIds": [layer.digest for layer in layers]})
        self.assertEqual(model_config["config"], {"format": "gguf", "paramSize": "4.9K", "quantization": "q8_0"})
        self.assertEqual(model_config["descriptor"], {"revision": COMMIT_A, "name": "tiny", "family": "tinyllama", "licenses": ["apache-2.0"]})
        self.assertEqual(main.package(files, revision=COMMIT_A)[2], manifest)
        with self.assertRaisesRegex(main.InputError, "both named"):
            main.package([(gguf, None), (gguf, None)])

    def test_references_need_a_registry_host_and_valid_names(self):
        self.assertEqual(main.parse_ref("localhost:5000/models/smollm2:q8_0"), main.Ref("localhost:5000", "models/smollm2", "q8_0"))
        self.assertEqual(main.parse_ref("registry.example.com/m").reference, "latest")
        digest = "sha256:" + "c" * 64
        self.assertEqual(main.parse_ref(f"localhost:5000/m@{digest}").reference, digest)
        for bad in ("models/smollm2:q8_0", "localhost:5000/Models:x", "localhost:5000/m:-bad", "localhost:5000/m@sha256:xyz"):
            with self.subTest(ref=bad):
                with self.assertRaises(main.InputError):
                    main.parse_ref(bad)

    def test_push_uploads_blobs_then_the_manifest_and_a_second_push_skips_every_blob(self):
        gguf, config, readme = self.model_files()
        handler = registry_handler()
        with serve(handler) as (_, port):
            first = main.push(main.parse_ref(f"127.0.0.1:{port}/models/tiny:q8_0"), [(gguf, None), (config, None), (readme, None)], plain_http=True)
            second = main.push(main.parse_ref(f"127.0.0.1:{port}/models/tiny:again"), [(gguf, None), (config, None), (readme, None)], plain_http=True)
        self.assertEqual(len(first["uploaded"]), 4)
        self.assertEqual(first["skipped"], [])
        self.assertEqual(second["uploaded"], [])
        self.assertEqual(len(second["skipped"]), 4)
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(first["pinned"], f"127.0.0.1:{port}/models/tiny@{first['digest']}")
        posts = [c for c in handler.calls if c.startswith("POST")]
        self.assertEqual(len(posts), 4)
        self.assertEqual(handler.calls[-1], "PUT /v2/models/tiny/manifests/again")

    def test_pull_restores_the_files_by_annotation_and_reads_the_model_config(self):
        gguf, config, _ = self.model_files()
        with serve(registry_handler()) as (_, port):
            pushed = main.push(main.parse_ref(f"127.0.0.1:{port}/models/tiny:q8_0"), [(gguf, None), (config, None)], plain_http=True, revision=COMMIT_A)
            code, out, err = run(["pull", f"127.0.0.1:{port}/models/tiny@{pushed['digest']}", "--dest", os.path.join(self.dir, "out"), "--plain-http", "--json"])
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["artifactType"], main.MODEL_ARTIFACT)
        self.assertEqual(result["modelConfig"]["descriptor"]["revision"], COMMIT_A)
        for original in (gguf, config):
            with open(original, "rb") as a, open(os.path.join(self.dir, "out", os.path.basename(original)), "rb") as b:
                self.assertEqual(a.read(), b.read())

    def test_pull_refuses_a_tampered_blob_and_a_path_that_escapes_the_destination(self):
        gguf, config, _ = self.model_files()
        with serve(registry_handler(tamper=True)) as (_, port):
            main.push(main.parse_ref(f"127.0.0.1:{port}/models/tiny:q8_0"), [(gguf, None)], plain_http=True)
            code, _, err = run(["pull", f"127.0.0.1:{port}/models/tiny:q8_0", "--dest", os.path.join(self.dir, "out"), "--plain-http"])
        self.assertEqual(code, 1)
        self.assertIn("nothing was written", err)
        self.assertEqual(os.listdir(os.path.join(self.dir, "out")), [])
        handler = registry_handler()
        with serve(handler) as (_, port):
            main.push(main.parse_ref(f"127.0.0.1:{port}/models/tiny:q8_0"), [(config, None)], plain_http=True)
            document = json.loads(handler.manifests[("models/tiny", "q8_0")])
            document["layers"][0]["annotations"] = {main.TITLE: "../../escaped.json"}
            handler.manifests[("models/tiny", "evil")] = json.dumps(document).encode()
            code, _, err = run(["pull", f"127.0.0.1:{port}/models/tiny:evil", "--dest", os.path.join(self.dir, "out2"), "--plain-http"])
        self.assertEqual(code, 1)
        self.assertIn("refusing path", err)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "escaped.json")))

    def test_dry_run_prints_the_manifest_and_writes_the_config_without_a_registry(self):
        gguf, config, _ = self.model_files()
        config_out = os.path.join(self.dir, "modelpack-config.json")
        code, out, err = run(["push", "localhost:5999/models/tiny:q8_0", gguf, f"{config}:{main.WEIGHT_CONFIG_RAW}", "--dry-run", "--config-out", config_out])
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        with open(config_out, "rb") as handle:
            self.assertEqual(result["manifest"]["config"]["digest"], "sha256:" + hashlib.sha256(handle.read()).hexdigest())
        code, _, err = run(["push", "127.0.0.1:1/models/tiny:q8_0", gguf, "--plain-http"])
        self.assertEqual(code, 2)
        self.assertIn("is the registry running", err)


class CliTests(TempDirCase):
    def test_inspect_and_memory_json_agree_on_the_counts(self):
        path = self.write("tiny.gguf", make_gguf(tiny_llama_kvs(), tiny_llama_tensors()))
        code, out, err = run(["inspect", path, "--json", "--tensors"])
        self.assertEqual(code, 0, err)
        info = json.loads(out)
        self.assertEqual(info["parameters"], 4864)
        self.assertEqual(info["shape"]["kv_bytes_per_token_f16"], 256)
        self.assertEqual(info["metadata"]["tokenizer.ggml.tokens"]["count"], 3)
        self.assertEqual(len(info["tensors"]), 4)
        code, out, err = run(["memory", path, "--ctx", "128", "--json"])
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["this_file"]["bytes"], info["tensor_bytes"])
        self.assertEqual(report["kv_cache"]["bytes"], 256 * 128)
        self.assertEqual(report["total_bytes"], info["tensor_bytes"] + 256 * 128)
        code, out, _ = run(["memory", path])
        self.assertEqual(code, 0)
        self.assertIn("bytes/token", out)

    def test_memory_from_a_config_needs_a_parameter_count(self):
        config = self.write("config.json", json.dumps(MemoryTests.LLAMA_3_8B))
        self.assertEqual(run(["memory", config])[0], 2)
        code, out, _ = run(["memory", config, "--params", "8e9", "--quant", "Q4_K", "--ctx", "8192", "--gpu-gib", "24", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["weights_used"], {"label": "Q4_K", "bytes": 4_500_000_000})


if __name__ == "__main__":
    unittest.main()
