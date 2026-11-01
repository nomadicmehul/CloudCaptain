"""modelinspect: read safetensors and GGUF headers without loading the weights, count parameters and bytes per dtype, estimate weight and KV cache memory, verify digests against a pinned Hugging Face revision, and push or pull a model as an OCI artifact with plain HTTP."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import re
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

GGUF_MAGIC = b"GGUF"
GGUF_DEFAULT_ALIGNMENT = 32
GGML_MAX_DIMS = 4
SAFETENSORS_MAX_HEADER = 100_000_000
FIRST_READ = 64 << 10
MAX_READ = 8 << 20
COPY_CHUNK = 1 << 20
USER_AGENT = "modelinspect/1.0"
HF_ENDPOINT = "https://huggingface.co"
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
GGUF_SCALARS: Dict[int, Tuple[str, str]] = {
    0: ("UINT8", "<B"),
    1: ("INT8", "<b"),
    2: ("UINT16", "<H"),
    3: ("INT16", "<h"),
    4: ("UINT32", "<I"),
    5: ("INT32", "<i"),
    6: ("FLOAT32", "<f"),
    7: ("BOOL", "<?"),
    10: ("UINT64", "<Q"),
    11: ("INT64", "<q"),
    12: ("FLOAT64", "<d"),
}
GGUF_STRING = 8
GGUF_ARRAY = 9
GGML_TYPES: Dict[int, Tuple[str, int, int]] = {
    0: ("F32", 1, 4),
    1: ("F16", 1, 2),
    2: ("Q4_0", 32, 18),
    3: ("Q4_1", 32, 20),
    6: ("Q5_0", 32, 22),
    7: ("Q5_1", 32, 24),
    8: ("Q8_0", 32, 34),
    10: ("Q2_K", 256, 84),
    11: ("Q3_K", 256, 110),
    12: ("Q4_K", 256, 144),
    13: ("Q5_K", 256, 176),
    14: ("Q6_K", 256, 210),
    15: ("Q8_K", 256, 292),
    16: ("IQ2_XXS", 256, 66),
    17: ("IQ2_XS", 256, 74),
    18: ("IQ3_XXS", 256, 98),
    19: ("IQ1_S", 256, 50),
    20: ("IQ4_NL", 32, 18),
    21: ("IQ3_S", 256, 110),
    22: ("IQ2_S", 256, 82),
    23: ("IQ4_XS", 256, 136),
    24: ("I8", 1, 1),
    25: ("I16", 1, 2),
    26: ("I32", 1, 4),
    27: ("I64", 1, 8),
    28: ("F64", 1, 8),
    29: ("IQ1_M", 256, 56),
    30: ("BF16", 1, 2),
    34: ("TQ1_0", 256, 54),
    35: ("TQ2_0", 256, 66),
    39: ("MXFP4", 32, 17),
}
BLOCK_FORMATS: Dict[str, Tuple[int, int]] = {name: (block, size) for name, block, size in GGML_TYPES.values()}
SAFETENSORS_BITS: Dict[str, int] = {
    "BOOL": 8,
    "F4": 4,
    "F6_E2M3": 6,
    "F6_E3M2": 6,
    "U8": 8,
    "I8": 8,
    "F8_E5M2": 8,
    "F8_E4M3": 8,
    "F8_E8M0": 8,
    "F8_E4M3FNUZ": 8,
    "F8_E5M2FNUZ": 8,
    "I16": 16,
    "U16": 16,
    "F16": 16,
    "BF16": 16,
    "I32": 32,
    "U32": 32,
    "F32": 32,
    "C64": 64,
    "F64": 64,
    "I64": 64,
    "U64": 64,
}
PRECISIONS = {
    "F64": "float64",
    "F32": "float32",
    "F16": "float16",
    "BF16": "bfloat16",
    "F8_E4M3": "float8_e4m3",
    "F8_E5M2": "float8_e5m2",
    "I8": "int8",
    "U8": "uint8",
    "I16": "int16",
    "I32": "int32",
    "I64": "int64",
    "BOOL": "bool",
}
LLAMA_FILE_TYPES = {
    0: "ALL_F32",
    1: "MOSTLY_F16",
    2: "MOSTLY_Q4_0",
    3: "MOSTLY_Q4_1",
    7: "MOSTLY_Q8_0",
    8: "MOSTLY_Q5_0",
    9: "MOSTLY_Q5_1",
    10: "MOSTLY_Q2_K",
    11: "MOSTLY_Q3_K_S",
    12: "MOSTLY_Q3_K_M",
    13: "MOSTLY_Q3_K_L",
    14: "MOSTLY_Q4_K_S",
    15: "MOSTLY_Q4_K_M",
    16: "MOSTLY_Q5_K_S",
    17: "MOSTLY_Q5_K_M",
    18: "MOSTLY_Q6_K",
    19: "MOSTLY_IQ2_XXS",
    20: "MOSTLY_IQ2_XS",
    21: "MOSTLY_Q2_K_S",
    22: "MOSTLY_IQ3_XS",
    23: "MOSTLY_IQ3_XXS",
    24: "MOSTLY_IQ1_S",
    25: "MOSTLY_IQ4_NL",
    26: "MOSTLY_IQ3_S",
    27: "MOSTLY_IQ3_M",
    28: "MOSTLY_IQ2_S",
    29: "MOSTLY_IQ2_M",
    30: "MOSTLY_IQ4_XS",
    31: "MOSTLY_IQ1_M",
    32: "MOSTLY_BF16",
    36: "MOSTLY_TQ1_0",
    37: "MOSTLY_TQ2_0",
}
MEMORY_FORMATS = ("F32", "BF16", "Q8_0", "Q6_K", "Q5_K", "Q4_K", "Q4_0", "Q3_K", "Q2_K")
KV_TYPES = ("f32", "f16", "bf16", "q8_0", "q5_1", "q5_0", "q4_1", "q4_0", "iq4_nl")
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
MODEL_ARTIFACT = "application/vnd.cncf.model.manifest.v1+json"
MODEL_CONFIG = "application/vnd.cncf.model.config.v1+json"
WEIGHT_RAW = "application/vnd.cncf.model.weight.v1.raw"
WEIGHT_CONFIG_RAW = "application/vnd.cncf.model.weight.config.v1.raw"
DOC_RAW = "application/vnd.cncf.model.doc.v1.raw"
CODE_RAW = "application/vnd.cncf.model.code.v1.raw"
TITLE = "org.opencontainers.image.title"
FILEPATH = "org.cncf.model.filepath"
UNTESTED = "org.cncf.model.file.mediatype.untested"
WEIGHT_SUFFIXES = (".gguf", ".safetensors", ".bin", ".pt", ".pth", ".onnx", ".ckpt", ".h5", ".msgpack", ".tflite")
WEIGHT_CONFIG_NAMES = ("config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.json", "merges.txt", "tokenizer.model", "added_tokens.json", "preprocessor_config.json", "chat_template.jinja")
DOC_SUFFIXES = (".md", ".txt", ".rst")
CODE_SUFFIXES = (".py", ".sh", ".ipynb")
PARAM_PREFIXES = (("Q", 10**15), ("T", 10**12), ("B", 10**9), ("M", 10**6), ("K", 10**3))


class InputError(ValueError):
    pass


class VerifyError(RuntimeError):
    pass


@dataclass
class Tensor:
    name: str
    dtype: str
    shape: List[int]
    offset: int
    nbytes: int

    @property
    def elements(self) -> int:
        return math.prod(self.shape) if self.shape else 1


@dataclass
class ModelFile:
    format: str
    source: str
    version: Optional[int]
    metadata: Dict[str, Any]
    value_types: Dict[str, str]
    tensors: List[Tensor]
    data_offset: int
    alignment: int
    file_size: Optional[int]

    @property
    def parameters(self) -> int:
        return sum(t.elements for t in self.tensors)

    @property
    def tensor_bytes(self) -> int:
        return sum(t.nbytes for t in self.tensors)

    def by_dtype(self) -> Dict[str, Dict[str, int]]:
        table: Dict[str, Dict[str, int]] = {}
        for tensor in self.tensors:
            row = table.setdefault(tensor.dtype, {"tensors": 0, "parameters": 0, "bytes": 0})
            row["tensors"] += 1
            row["parameters"] += tensor.elements
            row["bytes"] += tensor.nbytes
        return dict(sorted(table.items(), key=lambda item: -item[1]["bytes"]))

    def tensor(self, name: str) -> Tensor:
        for tensor in self.tensors:
            if tensor.name == name:
                return tensor
        raise InputError(f"no tensor named {name!r}; the first ones are: {', '.join(t.name for t in self.tensors[:5])}")


@dataclass
class Shape:
    architecture: str
    layers: int
    heads: int
    kv_heads: List[int]
    key_dim: int
    value_dim: int
    embedding: int
    context_length: int
    vocab: int
    notes: List[str] = field(default_factory=list)

    @property
    def kv_elements_per_token(self) -> int:
        return sum(heads * (self.key_dim + self.value_dim) for heads in self.kv_heads)


class FileSource:
    def __init__(self, path: str) -> None:
        if not os.path.isfile(path):
            raise InputError(f"{path}: no such file")
        self.name = path
        self.size: Optional[int] = os.path.getsize(path)
        self.requests = 0
        self.fetched = 0

    def read_at(self, offset: int, length: int) -> bytes:
        self.requests += 1
        with open(self.name, "rb") as handle:
            handle.seek(offset)
            data = handle.read(length)
        self.fetched += len(data)
        return data


class HttpSource:
    def __init__(self, url: str, timeout: float = 60.0) -> None:
        self.name = url
        self.size: Optional[int] = None
        self.requests = 0
        self.fetched = 0
        self.timeout = timeout

    def read_at(self, offset: int, length: int) -> bytes:
        if self.size is not None and offset >= self.size:
            return b""
        request = urllib.request.Request(self.name, headers={"Range": f"bytes={offset}-{offset + length - 1}", "User-Agent": USER_AGENT})
        self.requests += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 206:
                    raise InputError(f"{self.name} answered {response.status} to a Range request; download the file and inspect it locally")
                total = (response.headers.get("Content-Range") or "").rpartition("/")[2]
                if total.isdigit():
                    self.size = int(total)
                data = response.read()
        except urllib.error.HTTPError as error:
            if error.code == 416:
                return b""
            raise InputError(f"{self.name}: HTTP {error.code} {error.reason}")
        except urllib.error.URLError as error:
            raise InputError(f"{self.name}: {error.reason}")
        self.fetched += len(data)
        return data


class Reader:
    def __init__(self, source: Any, chunk: int = FIRST_READ, max_chunk: int = MAX_READ) -> None:
        self.source = source
        self.chunk = chunk
        self.max_chunk = max_chunk
        self.pos = 0
        self.buffer = b""
        self.buffer_start = 0

    def remaining(self) -> Optional[int]:
        return None if self.source.size is None else self.source.size - self.pos

    def read(self, n: int) -> bytes:
        if n < 0:
            raise InputError(f"negative length {n} at byte {self.pos}")
        left = self.remaining()
        if left is not None and n > left:
            raise InputError(f"truncated or corrupt: wants {n:,} bytes at byte {self.pos:,} but only {left:,} remain")
        end = self.pos + n
        if not (self.buffer_start <= self.pos and end <= self.buffer_start + len(self.buffer)):
            self.buffer = self.source.read_at(self.pos, max(n, self.chunk))
            self.buffer_start = self.pos
            self.chunk = min(self.chunk * 2, self.max_chunk)
        start = self.pos - self.buffer_start
        data = self.buffer[start:start + n]
        if len(data) != n:
            raise InputError(f"truncated or corrupt: unexpected end of file at byte {self.pos:,}")
        self.pos = end
        return data

    def unpack(self, fmt: str) -> Any:
        return struct.unpack(fmt, self.read(struct.calcsize(fmt)))[0]


def open_source(location: str) -> Any:
    if location.startswith(("http://", "https://")):
        return HttpSource(location)
    return FileSource(location)


def align(offset: int, alignment: int) -> int:
    return offset + (alignment - offset % alignment) % alignment


def tensor_nbytes(dtype: str, elements: int) -> int:
    block, size = BLOCK_FORMATS[dtype]
    return elements // block * size


def read_gguf_string(reader: Reader) -> str:
    length = reader.unpack("<Q")
    return reader.read(length).decode("utf-8", errors="replace")


def read_gguf_value(reader: Reader, value_type: int, depth: int = 0) -> Any:
    if value_type in GGUF_SCALARS:
        return reader.unpack(GGUF_SCALARS[value_type][1])
    if value_type == GGUF_STRING:
        return read_gguf_string(reader)
    if value_type == GGUF_ARRAY:
        if depth > 2:
            raise InputError(f"arrays nested deeper than 3 levels at byte {reader.pos:,}")
        item_type = reader.unpack("<I")
        count = reader.unpack("<Q")
        smallest = struct.calcsize(GGUF_SCALARS[item_type][1]) if item_type in GGUF_SCALARS else 8
        left = reader.remaining()
        if left is not None and count * smallest > left:
            raise InputError(f"array of {count:,} items cannot fit in the {left:,} bytes left at byte {reader.pos:,}")
        if item_type in GGUF_SCALARS:
            fmt = f"<{count}{GGUF_SCALARS[item_type][1][1]}"
            return list(struct.unpack(fmt, reader.read(struct.calcsize(fmt))))
        return [read_gguf_value(reader, item_type, depth + 1) for _ in range(count)]
    raise InputError(f"unknown GGUF value type {value_type} at byte {reader.pos - 4:,}")


def gguf_type_name(value_type: int) -> str:
    if value_type == GGUF_STRING:
        return "STRING"
    if value_type == GGUF_ARRAY:
        return "ARRAY"
    return GGUF_SCALARS[value_type][0]


def parse_gguf(reader: Reader) -> ModelFile:
    if reader.read(4) != GGUF_MAGIC:
        raise InputError("not a GGUF file: the first four bytes are not 'GGUF'")
    raw_version = reader.read(4)
    version = struct.unpack("<I", raw_version)[0]
    if version not in (2, 3):
        if struct.unpack(">I", raw_version)[0] in (2, 3):
            raise InputError("a big-endian GGUF file; modelinspect reads little-endian files only")
        if version == 1:
            raise InputError("GGUF version 1 (32-bit counts) is obsolete; convert the model again with a current llama.cpp")
        raise InputError(f"unsupported GGUF version {version}")
    tensor_count = reader.unpack("<Q")
    kv_count = reader.unpack("<Q")
    left = reader.remaining()
    if left is not None and (kv_count * 13 > left or tensor_count * 24 > left):
        raise InputError(f"header claims {kv_count:,} metadata keys and {tensor_count:,} tensors, more than {left:,} bytes can hold")
    metadata: Dict[str, Any] = {}
    value_types: Dict[str, str] = {}
    for _ in range(kv_count):
        key = read_gguf_string(reader)
        value_type = reader.unpack("<I")
        if key in metadata:
            raise InputError(f"duplicate metadata key {key!r}")
        metadata[key] = read_gguf_value(reader, value_type)
        value_types[key] = gguf_type_name(value_type)
    alignment = metadata.get("general.alignment", GGUF_DEFAULT_ALIGNMENT)
    if not isinstance(alignment, int) or alignment <= 0 or alignment & (alignment - 1):
        raise InputError(f"general.alignment must be a power of two, got {alignment!r}")
    infos: List[Tuple[str, str, List[int], int, int]] = []
    names = set()
    for _ in range(tensor_count):
        name = read_gguf_string(reader)
        n_dims = reader.unpack("<I")
        if n_dims > GGML_MAX_DIMS:
            raise InputError(f"tensor {name!r} has {n_dims} dimensions; ggml allows at most {GGML_MAX_DIMS}")
        dims = [reader.unpack("<Q") for _ in range(n_dims)]
        type_id = reader.unpack("<I")
        offset = reader.unpack("<Q")
        if type_id not in GGML_TYPES:
            raise InputError(f"tensor {name!r} has unknown ggml type {type_id}")
        dtype, block, _ = GGML_TYPES[type_id]
        if name in names:
            raise InputError(f"duplicate tensor name {name!r}")
        names.add(name)
        elements = math.prod(dims) if dims else 1
        if elements >= 1 << 62:
            raise InputError(f"tensor {name!r} claims {elements:,} elements")
        if dims and dims[0] % block:
            raise InputError(f"tensor {name!r}: row length {dims[0]} is not a multiple of the {dtype} block size {block}")
        if offset % alignment:
            raise InputError(f"tensor {name!r}: data offset {offset} is not aligned to {alignment}")
        infos.append((name, dtype, dims, offset, tensor_nbytes(dtype, elements)))
    data_offset = align(reader.pos, alignment)
    tensors = [Tensor(name, dtype, dims, data_offset + offset, nbytes) for name, dtype, dims, offset, nbytes in infos]
    check_layout(tensors, reader.source.size)
    return ModelFile("gguf", reader.source.name, version, metadata, value_types, tensors, data_offset, alignment, reader.source.size)


def check_layout(tensors: Sequence[Tensor], file_size: Optional[int]) -> None:
    previous: Optional[Tensor] = None
    for tensor in sorted(tensors, key=lambda t: t.offset):
        if previous is not None and tensor.offset < previous.offset + previous.nbytes:
            raise InputError(f"tensor {tensor.name!r} overlaps {previous.name!r}")
        if file_size is not None and tensor.offset + tensor.nbytes > file_size:
            raise InputError(f"tensor {tensor.name!r} ends at byte {tensor.offset + tensor.nbytes:,}, past the end of the file ({file_size:,} bytes)")
        previous = tensor


def parse_safetensors(reader: Reader) -> ModelFile:
    header_size = reader.unpack("<Q")
    if header_size > SAFETENSORS_MAX_HEADER:
        raise InputError(f"header claims {header_size:,} bytes; safetensors refuses headers above {SAFETENSORS_MAX_HEADER:,}")
    raw = reader.read(header_size)
    if not raw.startswith(b"{"):
        raise InputError("not a safetensors file: the JSON header does not start with '{'")
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"safetensors header is not valid JSON: {exc}")
    if not isinstance(header, dict):
        raise InputError("safetensors header must be a JSON object")
    metadata = header.pop("__metadata__", None) or {}
    if not isinstance(metadata, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()):
        raise InputError("__metadata__ must map strings to strings")
    data_offset = 8 + header_size
    entries: List[Tuple[int, int, Tensor]] = []
    for name, info in header.items():
        if not isinstance(info, dict):
            raise InputError(f"tensor {name!r}: entry is not an object")
        dtype, shape, offsets = info.get("dtype"), info.get("shape"), info.get("data_offsets")
        if dtype not in SAFETENSORS_BITS:
            raise InputError(f"tensor {name!r}: unknown dtype {dtype!r}")
        if not isinstance(shape, list) or not all(isinstance(d, int) and d >= 0 for d in shape):
            raise InputError(f"tensor {name!r}: shape must be a list of non-negative integers")
        if not isinstance(offsets, list) or len(offsets) != 2 or not all(isinstance(o, int) for o in offsets):
            raise InputError(f"tensor {name!r}: data_offsets must be [begin, end]")
        begin, end = offsets
        bits = (math.prod(shape) if shape else 1) * SAFETENSORS_BITS[dtype]
        if bits % 8:
            raise InputError(f"tensor {name!r}: {dtype} x {shape} is not a whole number of bytes")
        if end - begin != bits // 8:
            raise InputError(f"tensor {name!r}: data_offsets span {end - begin} bytes but {dtype} x {shape} needs {bits // 8}")
        entries.append((begin, end, Tensor(name, dtype, list(shape), data_offset + begin, end - begin)))
    entries.sort(key=lambda e: (e[0], e[1]))
    cursor = 0
    for begin, end, tensor in entries:
        if begin != cursor:
            raise InputError(f"tensor {tensor.name!r} starts at {begin} but the previous tensor ended at {cursor}: safetensors data must be contiguous")
        cursor = end
    size = reader.source.size
    if size is not None and data_offset + cursor != size:
        raise InputError(f"file is {size:,} bytes but header and tensors account for {data_offset + cursor:,}")
    tensors = [entry[2] for entry in entries]
    value_types = {key: "STRING" for key in metadata}
    return ModelFile("safetensors", reader.source.name, None, dict(metadata), value_types, tensors, data_offset, 1, size)


def sniff(head: bytes) -> str:
    if head[:4] == GGUF_MAGIC:
        return "gguf"
    if len(head) >= 9 and head[8:9] == b"{":
        return "safetensors"
    if head[:4] == b"PK\x03\x04":
        return "zip"
    if head[:1] == b"\x80" and head[1:2] in (b"\x02", b"\x03", b"\x04", b"\x05"):
        return "pickle"
    return "unknown"


def load_model(location: str, chunk: int = FIRST_READ) -> Tuple[ModelFile, Any]:
    source = open_source(location)
    reader = Reader(source, chunk)
    left = reader.remaining()
    kind = sniff(reader.read(16 if left is None else min(16, left)))
    reader.pos = 0
    if kind == "gguf":
        return parse_gguf(reader), source
    if kind == "safetensors":
        return parse_safetensors(reader), source
    if kind in ("zip", "pickle"):
        raise InputError(f"{location}: a PyTorch checkpoint ({kind}). Loading it runs pickle, which can execute code; modelinspect does not unpickle. Ask for safetensors or convert in a sandbox.")
    raise InputError(f"{location}: neither GGUF nor safetensors")


def arch_key(metadata: Dict[str, Any], suffix: str) -> Any:
    return metadata.get(f"{metadata.get('general.architecture', '')}.{suffix}")


def as_layer_list(value: Any, layers: int, default: int) -> List[int]:
    if isinstance(value, list):
        if len(value) != layers:
            raise InputError(f"per-layer list has {len(value)} entries for {layers} layers")
        return [int(v) for v in value]
    return [int(value if value is not None else default)] * layers


def gguf_shape(model: ModelFile) -> Optional[Shape]:
    md = model.metadata
    architecture = md.get("general.architecture")
    layers = arch_key(md, "block_count")
    embedding = arch_key(md, "embedding_length")
    heads_value = arch_key(md, "attention.head_count")
    if not architecture or not layers or not embedding or heads_value is None:
        return None
    heads = max(heads_value) if isinstance(heads_value, list) else int(heads_value)
    kv_value = arch_key(md, "attention.head_count_kv")
    kv_heads = as_layer_list(kv_value, layers, heads)
    key_dim = int(arch_key(md, "attention.key_length") or embedding // heads)
    value_dim = int(arch_key(md, "attention.value_length") or key_dim)
    tokens = md.get("tokenizer.ggml.tokens")
    vocab = int(arch_key(md, "vocab_size") or (len(tokens) if isinstance(tokens, list) else 0))
    notes = []
    if arch_key(md, "attention.sliding_window"):
        notes.append(f"sliding window {arch_key(md, 'attention.sliding_window')}: runtimes with a SWA cache keep fewer tokens for some layers")
    if arch_key(md, "attention.kv_lora_rank"):
        notes.append("latent attention (MLA): the cache holds a compressed latent, so this estimate is an upper bound")
    return Shape(architecture, int(layers), heads, kv_heads, key_dim, value_dim, int(embedding), int(arch_key(md, "context_length") or 0), vocab, notes)


def config_shape(config: Dict[str, Any]) -> Shape:
    text = config.get("text_config") if isinstance(config.get("text_config"), dict) else config
    layers = text.get("num_hidden_layers") or text.get("n_layer") or text.get("num_layers")
    heads = text.get("num_attention_heads") or text.get("n_head")
    hidden = text.get("hidden_size") or text.get("n_embd") or text.get("d_model")
    if not layers or not heads or not hidden:
        raise InputError("config.json needs num_hidden_layers, num_attention_heads and hidden_size (or their GPT-2 names)")
    kv = text.get("num_key_value_heads") or heads
    head_dim = text.get("head_dim") or hidden // heads
    architecture = text.get("model_type") or (config.get("architectures") or ["unknown"])[0]
    context = text.get("max_position_embeddings") or text.get("n_positions") or 0
    notes = []
    if text.get("sliding_window"):
        notes.append(f"sliding window {text['sliding_window']}: runtimes with a SWA cache keep fewer tokens for some layers")
    if text.get("kv_lora_rank"):
        notes.append("latent attention (MLA): the cache holds a compressed latent, so this estimate is an upper bound")
    return Shape(str(architecture), int(layers), int(heads), [int(kv)] * int(layers), int(head_dim), int(head_dim), int(hidden), int(context), int(text.get("vocab_size") or 0), notes)


def read_config(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise InputError(f"{path}: {exc.strerror}")
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: not JSON ({exc})")


def model_shape(model: Optional[ModelFile], config_path: Optional[str]) -> Optional[Shape]:
    if config_path:
        return config_shape(read_config(config_path))
    if model is not None and model.format == "gguf":
        return gguf_shape(model)
    if model is not None and not model.source.startswith(("http://", "https://")):
        sibling = os.path.join(os.path.dirname(os.path.abspath(model.source)), "config.json")
        if os.path.isfile(sibling):
            return config_shape(read_config(sibling))
    return None


def file_type_label(model: ModelFile) -> Optional[str]:
    value = model.metadata.get("general.file_type")
    return LLAMA_FILE_TYPES.get(value, f"type {value}") if isinstance(value, int) else None


def bits_per_weight(dtype: str) -> float:
    if dtype in BLOCK_FORMATS:
        block, size = BLOCK_FORMATS[dtype]
        return size * 8 / block
    return float(SAFETENSORS_BITS[dtype])


def weight_bytes(parameters: int, dtype: str) -> int:
    key = dtype.upper()
    if key in BLOCK_FORMATS:
        block, size = BLOCK_FORMATS[key]
        return -(-parameters // block) * size
    if key in SAFETENSORS_BITS:
        return -(-parameters * SAFETENSORS_BITS[key] // 8)
    raise InputError(f"unknown format {dtype!r}; known: {', '.join(sorted(set(BLOCK_FORMATS) | set(SAFETENSORS_BITS)))}")


def kv_format(kv_type: str) -> Tuple[int, int]:
    if kv_type.lower() not in KV_TYPES:
        raise InputError(f"unknown KV cache type {kv_type!r}; llama.cpp accepts {', '.join(KV_TYPES)}")
    return BLOCK_FORMATS[kv_type.upper()]


def kv_bytes_per_token(shape: Shape, kv_type: str = "f16") -> float:
    block, size = kv_format(kv_type)
    return shape.kv_elements_per_token * size / block


def kv_cache_bytes(shape: Shape, tokens: int, kv_type: str = "f16") -> int:
    block, size = kv_format(kv_type)
    if shape.key_dim % block or shape.value_dim % block:
        raise InputError(f"head dimensions {shape.key_dim}/{shape.value_dim} are not multiples of the {kv_type} block size {block}")
    return shape.kv_elements_per_token * tokens * size // block


def param_size_label(parameters: int) -> str:
    for prefix, scale in PARAM_PREFIXES:
        if parameters >= scale:
            count = f"{parameters / scale:.1f}"
            return (count[:-2] if count.endswith(".0") else count) + prefix
    return str(parameters)


def fp16_to_float(raw: int) -> float:
    return struct.unpack("<e", struct.pack("<H", raw))[0]


def bf16_to_float(raw: int) -> float:
    return struct.unpack("<f", struct.pack("<I", raw << 16))[0]


def dequantize_q8_0(block: bytes) -> List[float]:
    if len(block) != 34:
        raise InputError(f"a Q8_0 block is 34 bytes, got {len(block)}")
    scale = fp16_to_float(struct.unpack("<H", block[:2])[0])
    return [q * scale for q in struct.unpack("<32b", block[2:])]


def dequantize_q4_0(block: bytes) -> List[float]:
    if len(block) != 18:
        raise InputError(f"a Q4_0 block is 18 bytes, got {len(block)}")
    scale = fp16_to_float(struct.unpack("<H", block[:2])[0])
    low = [((byte & 0x0F) - 8) * scale for byte in block[2:]]
    high = [((byte >> 4) - 8) * scale for byte in block[2:]]
    return low + high


def float_to_fp16_bits(value: float) -> int:
    return struct.unpack("<H", struct.pack("<e", value))[0]


def quantize_q8_0(values: Sequence[float]) -> bytes:
    if len(values) != 32:
        raise InputError("Q8_0 quantizes blocks of 32 values")
    amax = max(abs(v) for v in values)
    scale = amax / 127 if amax else 0.0
    inverse = 1 / scale if scale else 0.0
    quants = [max(-128, min(127, int(round(v * inverse)))) for v in values]
    return struct.pack("<H", float_to_fp16_bits(scale)) + struct.pack("<32b", *quants)


def quantize_q4_0(values: Sequence[float]) -> bytes:
    if len(values) != 32:
        raise InputError("Q4_0 quantizes blocks of 32 values")
    extreme = max(values, key=abs)
    scale = extreme / -8 if extreme else 0.0
    inverse = 1 / scale if scale else 0.0
    nibbles = [min(15, int(v * inverse + 8.5)) for v in values]
    packed = bytes(nibbles[i] | (nibbles[i + 16] << 4) for i in range(16))
    return struct.pack("<H", float_to_fp16_bits(scale)) + packed


def decode_values(dtype: str, data: bytes, count: int) -> List[float]:
    if dtype == "F32":
        return list(struct.unpack(f"<{count}f", data[:4 * count]))
    if dtype == "F16":
        return list(struct.unpack(f"<{count}e", data[:2 * count]))
    if dtype == "BF16":
        return [bf16_to_float(raw) for raw in struct.unpack(f"<{count}H", data[:2 * count])]
    if dtype in ("Q8_0", "Q4_0"):
        block, size = BLOCK_FORMATS[dtype]
        decode = dequantize_q8_0 if dtype == "Q8_0" else dequantize_q4_0
        values: List[float] = []
        for start in range(0, len(data) - size + 1, size):
            values.extend(decode(data[start:start + size]))
        return values[:count]
    raise InputError(f"--peek decodes F32, F16, BF16, Q8_0 and Q4_0; {dtype} is a K-quant or exotic type")


def peek(model: ModelFile, source: Any, name: str, count: int) -> List[float]:
    tensor = model.tensor(name)
    count = max(0, min(count, tensor.elements))
    if tensor.dtype in BLOCK_FORMATS:
        block, size = BLOCK_FORMATS[tensor.dtype]
        length = -(-count // block) * size
    else:
        length = -(-count * SAFETENSORS_BITS[tensor.dtype] // 8)
    return decode_values(tensor.dtype, source.read_at(tensor.offset, length), count)


def summarize_value(value: Any) -> Any:
    if isinstance(value, list):
        kinds = sorted({type(v).__name__ for v in value})
        return {"array": "/".join(kinds) or "empty", "count": len(value), "head": [summarize_value(v) for v in value[:3]]}
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + f"... ({len(value):,} chars)"
    return value


def inspection(model: ModelFile, source: Any, shape: Optional[Shape], with_tensors: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "source": model.source,
        "format": model.format,
        "version": model.version,
        "file_size": model.file_size,
        "data_offset": model.data_offset,
        "alignment": model.alignment,
        "tensor_count": len(model.tensors),
        "metadata_count": len(model.metadata),
        "parameters": model.parameters,
        "param_size": param_size_label(model.parameters),
        "tensor_bytes": model.tensor_bytes,
        "bits_per_weight": round(model.tensor_bytes * 8 / model.parameters, 4) if model.parameters else 0,
        "dtypes": model.by_dtype(),
        "file_type": file_type_label(model) if model.format == "gguf" else None,
        "metadata": {key: summarize_value(value) for key, value in model.metadata.items()},
        "shape": None if shape is None else {
            "architecture": shape.architecture,
            "layers": shape.layers,
            "heads": shape.heads,
            "kv_heads": shape.kv_heads[0] if len(set(shape.kv_heads)) == 1 else shape.kv_heads,
            "key_dim": shape.key_dim,
            "value_dim": shape.value_dim,
            "embedding": shape.embedding,
            "context_length": shape.context_length,
            "vocab": shape.vocab,
            "kv_bytes_per_token_f16": kv_cache_bytes(shape, 1, "f16"),
        },
        "reads": {"requests": source.requests, "bytes": source.fetched},
    }
    if with_tensors:
        result["tensors"] = [{"name": t.name, "dtype": t.dtype, "shape": t.shape, "offset": t.offset, "bytes": t.nbytes} for t in model.tensors]
    return result


def render_inspection(info: Dict[str, Any]) -> str:
    version = f" v{info['version']}" if info["version"] else ""
    lines = [f"{os.path.basename(info['source'])}: {info['format'].upper()}{version}, {info['metadata_count']} metadata keys, {info['tensor_count']} tensors, data at byte {info['data_offset']:,}"]
    md = info["metadata"]
    if info["format"] == "gguf":
        lines.append(f"model     {md.get('general.architecture', '?')} {md.get('general.name', '')!r}, file type {info['file_type'] or 'not set'}")
    shape = info["shape"]
    if shape:
        lines.append(f"shape     {shape['layers']} layers, embedding {shape['embedding']}, {shape['heads']} heads, {shape['kv_heads']} KV heads, head dim {shape['key_dim']}, context {shape['context_length']:,}, vocab {shape['vocab']:,}")
    lines.append(f"params    {info['parameters']:,} ({info['param_size']})")
    lines.append(f"  {'dtype':<8}{'tensors':>8}{'parameters':>16}{'bytes':>16}{'bits/weight':>13}")
    for dtype, row in info["dtypes"].items():
        lines.append(f"  {dtype:<8}{row['tensors']:>8}{row['parameters']:>16,}{row['bytes']:>16,}{row['bytes'] * 8 / max(row['parameters'], 1):>13.2f}")
    lines.append(f"  {'total':<8}{info['tensor_count']:>8}{info['parameters']:>16,}{info['tensor_bytes']:>16,}{info['bits_per_weight']:>13.2f}")
    if info["file_size"] is not None:
        padding = info["file_size"] - info["data_offset"] - info["tensor_bytes"]
        lines.append(f"file      {info['file_size']:,} bytes = {info['data_offset']:,} header + {info['tensor_bytes']:,} tensors + {padding:,} padding")
    reads = info["reads"]
    lines.append(f"read      {reads['bytes']:,} bytes in {reads['requests']} request(s)")
    for tensor in info.get("tensors", []):
        lines.append(f"  {tensor['name']:<40} {tensor['dtype']:<8} {str(tensor['shape']):<20} {tensor['bytes']:>14,}")
    return "\n".join(lines)


def memory_report(parameters: int, shape: Optional[Shape], formats: Sequence[str], ctx: int, seqs: int, kv_type: str = "f16", file_bytes: Optional[Tuple[str, int]] = None, selected: Optional[str] = None, gpu_gib: Optional[float] = None, utilization: float = 0.9) -> Dict[str, Any]:
    if ctx <= 0 or seqs <= 0:
        raise InputError("--ctx and --seqs must be positive")
    weights = {fmt.upper(): weight_bytes(parameters, fmt) for fmt in formats}
    report: Dict[str, Any] = {"parameters": parameters, "param_size": param_size_label(parameters), "weights": weights}
    if file_bytes is not None:
        report["this_file"] = {"label": file_bytes[0], "bytes": file_bytes[1]}
    if selected:
        used_label, chosen = selected.upper(), weight_bytes(parameters, selected)
    elif file_bytes is not None:
        used_label, chosen = "this file", file_bytes[1]
    else:
        used_label, chosen = "BF16", weight_bytes(parameters, "BF16")
    report["weights_used"] = {"label": used_label, "bytes": chosen}
    if shape is not None:
        per_token = kv_bytes_per_token(shape, kv_type)
        total_kv = kv_cache_bytes(shape, ctx * seqs, kv_type)
        report["kv_cache"] = {
            "type": kv_type.lower(),
            "layers": shape.layers,
            "kv_heads": shape.kv_heads[0] if len(set(shape.kv_heads)) == 1 else shape.kv_heads,
            "key_dim": shape.key_dim,
            "value_dim": shape.value_dim,
            "bytes_per_token": per_token,
            "tokens": ctx * seqs,
            "context": ctx,
            "sequences": seqs,
            "bytes": total_kv,
            "notes": shape.notes,
        }
        report["total_bytes"] = chosen + total_kv
        if gpu_gib:
            budget = int(gpu_gib * (1 << 30) * utilization) - chosen
            tokens = int(budget // per_token) if budget > 0 and per_token else 0
            report["gpu"] = {"gib": gpu_gib, "utilization": utilization, "kv_budget_bytes": max(budget, 0), "max_tokens": tokens, "max_sequences_at_context": tokens // ctx if ctx else 0}
    return report


def size_text(n: float) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} GiB"
    return f"{n / (1 << 20):.1f} MiB"


def render_memory(report: Dict[str, Any], label: str) -> str:
    lines = [f"{label}: {report['parameters']:,} parameters ({report['param_size']})", "weights"]
    if "this_file" in report:
        lines.append(f"  {'this file':<10}{report['this_file']['bytes']:>16,}  {size_text(report['this_file']['bytes']):>11}  {report['this_file']['label']}")
    for fmt, nbytes in report["weights"].items():
        lines.append(f"  {fmt:<10}{nbytes:>16,}  {size_text(nbytes):>11}  {bits_per_weight(fmt):.4g} bits/weight")
    kv = report.get("kv_cache")
    if kv is None:
        lines.append("kv cache  unknown: pass a GGUF file or --config config.json for layers and heads")
        return "\n".join(lines)
    lines.append(f"kv cache  {kv['type']}: {kv['layers']} layers x {kv['kv_heads']} KV heads x ({kv['key_dim']} + {kv['value_dim']}) dims = {kv['bytes_per_token']:,.0f} bytes/token")
    lines.append(f"          {kv['context']:,} tokens x {kv['sequences']} sequence(s) = {kv['bytes']:,} bytes ({size_text(kv['bytes'])})")
    for note in kv["notes"]:
        lines.append(f"          note: {note}")
    lines.append(f"total     {report['total_bytes']:,} bytes ({size_text(report['total_bytes'])}) = weights ({report['weights_used']['label']}) + KV cache; runtime buffers come on top")
    gpu = report.get("gpu")
    if gpu:
        lines.append(f"gpu       {gpu['gib']} GiB x {gpu['utilization']}: {size_text(gpu['kv_budget_bytes'])} left for KV = {gpu['max_tokens']:,} tokens = {gpu['max_sequences_at_context']} sequence(s) of {kv['context']:,}")
    return "\n".join(lines)


def normalize_digest(text: str) -> str:
    value = text.strip().strip('"').lower()
    if value.startswith("sha256:"):
        value = value[7:]
    if not SHA256_HEX.match(value):
        raise InputError(f"not a sha256 digest: {text!r} (64 hex characters, optionally prefixed with sha256:)")
    return "sha256:" + value


def file_digest(path: str) -> Tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(COPY_CHUNK), b""):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise InputError(f"{path}: {exc.strerror}")
    return "sha256:" + digest.hexdigest(), size


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def hub_headers(url: str) -> Dict[str, str]:
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(request, timeout=60) as response:
            return {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as error:
        if error.code in (301, 302, 303, 307, 308):
            return {k.lower(): v for k, v in error.headers.items()}
        if error.code in (401, 403, 404):
            reason = error.headers.get("x-error-code") or error.reason
            raise InputError(f"{url}: HTTP {error.code} {reason}; check the repository, file name and revision (gated and private repos need a token)")
        raise InputError(f"{url}: HTTP {error.code} {error.reason}")
    except urllib.error.URLError as error:
        raise InputError(f"{url}: {error.reason}")


def resolve_url(endpoint: str, repo: str, revision: str, filename: str) -> str:
    return f"{endpoint.rstrip('/')}/{repo}/resolve/{urllib.parse.quote(revision, safe='')}/{urllib.parse.quote(filename)}"


def fetch(repo: str, filename: str, revision: str, dest: str, expected_sha256: Optional[str] = None, endpoint: str = HF_ENDPOINT, log: Callable[[str], None] = lambda line: None) -> Dict[str, Any]:
    url = resolve_url(endpoint, repo, revision, filename)
    headers = hub_headers(url)
    commit = headers.get("x-repo-commit", "")
    linked = headers.get("x-linked-etag", headers.get("etag", "")).strip().strip('"').removeprefix("W/").strip('"')
    if COMMIT.match(revision) and commit and commit != revision:
        raise VerifyError(f"asked for revision {revision} but the Hub resolved {commit}")
    if not COMMIT.match(revision):
        log(f"warning: revision {revision!r} is a branch or tag and can move; it resolves to {commit or 'an unknown commit'} now, pin --revision {commit or '<commit>'}")
    pinned = normalize_digest(expected_sha256) if expected_sha256 else None
    hub_sha256 = "sha256:" + linked if SHA256_HEX.match(linked) else None
    if pinned and hub_sha256 and pinned != hub_sha256:
        raise VerifyError(f"{filename} at {commit or revision}: you pinned {pinned} but the Hub serves {hub_sha256}; nothing was downloaded")
    try:
        target = safe_join(dest, filename)
    except VerifyError as exc:
        raise InputError(str(exc))
    partial = target + ".part"
    sha256 = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=60) as response, open(partial, "wb") as out:
            for chunk in iter(lambda: response.read(COPY_CHUNK), b""):
                sha256.update(chunk)
                out.write(chunk)
                size += len(chunk)
    except (urllib.error.URLError, OSError) as exc:
        if os.path.exists(partial):
            os.remove(partial)
        raise InputError(f"{url}: download failed: {getattr(exc, 'reason', exc)}")
    actual = "sha256:" + sha256.hexdigest()
    checks = []
    if pinned:
        checks.append(("pinned sha256", pinned, actual))
    if hub_sha256:
        checks.append(("Hub LFS sha256 (x-linked-etag)", hub_sha256, actual))
    elif COMMIT.match(linked):
        with open(partial, "rb") as handle:
            checks.append(("Hub git blob id (x-linked-etag)", linked, git_blob_sha1(handle.read())))
    for label, want, got in checks:
        if want != got:
            os.remove(partial)
            raise VerifyError(f"{filename}: {label} is {want} but the bytes hash to {got}; the partial download was removed")
    os.replace(partial, target)
    return {"url": url, "repo": repo, "file": filename, "revision": revision, "commit": commit, "path": target, "size": size, "digest": actual, "verified_against": [label for label, _, _ in checks]}


def safe_join(root: str, relative: str) -> str:
    if not relative or relative.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", relative):
        raise VerifyError(f"refusing path {relative!r}: absolute paths are not allowed")
    parts = relative.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise VerifyError(f"refusing path {relative!r}: empty, '.' or '..' components could escape {root}")
    target = os.path.join(os.path.abspath(root), *parts)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    return target


@dataclass
class Blob:
    media_type: str
    digest: str
    size: int
    path: Optional[str] = None
    data: Optional[bytes] = None
    annotations: Dict[str, str] = field(default_factory=dict)

    def descriptor(self) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"mediaType": self.media_type, "digest": self.digest, "size": self.size}
        if self.annotations:
            entry["annotations"] = self.annotations
        return entry


def classify(name: str) -> Tuple[str, bool]:
    base = os.path.basename(name).lower()
    if base.endswith(WEIGHT_SUFFIXES):
        return WEIGHT_RAW, False
    if base in WEIGHT_CONFIG_NAMES or base.endswith(".index.json"):
        return WEIGHT_CONFIG_RAW, False
    if base.startswith(("readme", "license", "notice")) or base.endswith(DOC_SUFFIXES):
        return DOC_RAW, False
    if base.endswith(CODE_SUFFIXES):
        return CODE_RAW, False
    return WEIGHT_CONFIG_RAW, True


def split_file_arg(text: str) -> Tuple[str, Optional[str]]:
    path, sep, media_type = text.rpartition(":")
    if sep and "/" in media_type and path and not re.match(r"^[A-Za-z]$", path):
        return path, media_type
    return text, None


def file_blob(path: str, media_type: Optional[str] = None, name: Optional[str] = None) -> Blob:
    digest, size = file_digest(path)
    title = name or os.path.basename(path)
    guessed, untested = classify(title)
    annotations = {TITLE: title, FILEPATH: title}
    if media_type is None and untested:
        annotations[UNTESTED] = "true"
    return Blob(media_type or guessed, digest, size, path=path, annotations=annotations)


def bytes_blob(data: bytes, media_type: str) -> Blob:
    return Blob(media_type, "sha256:" + hashlib.sha256(data).hexdigest(), len(data), data=data)


def model_config(layers: Sequence[Blob], name: str = "", revision: str = "", inspected: Optional[ModelFile] = None) -> Dict[str, Any]:
    descriptor: Dict[str, Any] = {}
    if name:
        descriptor["name"] = name
    if revision:
        descriptor["revision"] = revision
    technical: Dict[str, Any] = {}
    if inspected is not None:
        technical["format"] = inspected.format
        technical["paramSize"] = param_size_label(inspected.parameters)
        if inspected.format == "gguf":
            for key, target in (("general.name", "name"), ("general.basename", "family"), ("general.license", "licenses")):
                value = inspected.metadata.get(key)
                if isinstance(value, str) and value and target not in descriptor:
                    descriptor[target] = [value] if target == "licenses" else value
            label = file_type_label(inspected)
            if label:
                technical["quantization"] = label.replace("MOSTLY_", "").replace("ALL_", "").lower()
        else:
            precisions = [PRECISIONS[d] for d in inspected.by_dtype() if d in PRECISIONS]
            if precisions:
                technical["precision"] = ",".join(precisions)
    return {"descriptor": descriptor, "config": technical, "modelfs": {"type": "layers", "diffIds": [layer.digest for layer in layers]}}


def build_manifest(config: Blob, layers: Sequence[Blob], annotations: Optional[Dict[str, str]] = None) -> bytes:
    manifest: Dict[str, Any] = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "artifactType": MODEL_ARTIFACT,
        "config": config.descriptor(),
        "layers": [layer.descriptor() for layer in layers],
    }
    if annotations:
        manifest["annotations"] = dict(annotations)
    return json.dumps(manifest, separators=(",", ":")).encode("utf-8")


def package(files: Sequence[Tuple[str, Optional[str]]], name: str = "", revision: str = "") -> Tuple[Blob, List[Blob], bytes]:
    layers = []
    titles = set()
    inspected: Optional[ModelFile] = None
    for path, media_type in files:
        blob = file_blob(path, media_type)
        if blob.annotations[TITLE] in titles:
            raise InputError(f"two files are both named {blob.annotations[TITLE]!r}; a pull could not tell them apart")
        titles.add(blob.annotations[TITLE])
        layers.append(blob)
        if inspected is None and blob.media_type == WEIGHT_RAW:
            try:
                inspected = load_model(path)[0]
            except InputError:
                inspected = None
    config = bytes_blob(json.dumps(model_config(layers, name, revision, inspected), separators=(",", ":"), sort_keys=True).encode("utf-8"), MODEL_CONFIG)
    return config, layers, build_manifest(config, layers)


@dataclass(frozen=True)
class Ref:
    registry: str
    repository: str
    reference: str

    def __str__(self) -> str:
        joiner = "@" if self.reference.startswith("sha256:") else ":"
        return f"{self.registry}/{self.repository}{joiner}{self.reference}"


def parse_ref(text: str) -> Ref:
    host, _, rest = text.partition("/")
    if not rest or not ("." in host or ":" in host or host == "localhost"):
        raise InputError(f"{text!r}: include the registry host, for example localhost:5000/models/smollm2:q8_0")
    if "@" in rest:
        repository, reference = rest.split("@", 1)
        normalize_digest(reference)
    else:
        last = rest.rsplit("/", 1)[-1]
        if ":" in last:
            repository, reference = rest.rsplit(":", 1)
        else:
            repository, reference = rest, "latest"
    if not re.match(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*$", repository):
        raise InputError(f"{repository!r} is not a valid repository name (lowercase, digits, '.', '_', '-' and '/')")
    if not reference.startswith("sha256:") and not re.match(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$", reference):
        raise InputError(f"{reference!r} is not a valid tag (letters, digits, '_', '.', '-', at most 128 characters)")
    return Ref(host, repository, reference)


Response = Tuple[int, Dict[str, str], bytes]


class Registry:
    def __init__(self, registry: str, plain_http: bool = False, timeout: float = 300.0) -> None:
        self.base = ("http" if plain_http else "https") + "://" + registry
        self.timeout = timeout
        self.log: List[str] = []

    def url(self, repository: str, tail: str) -> str:
        return f"{self.base}/v2/{repository}/{tail}"

    def request(self, method: str, url: str, headers: Optional[Dict[str, str]] = None, body: Any = None, sink: Optional[Callable[[bytes], None]] = None, redirects: int = 5) -> Response:
        parts = urllib.parse.urlsplit(url)
        connection_class = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        connection = connection_class(parts.netloc, timeout=self.timeout, blocksize=COPY_CHUNK)
        path = parts.path + ("?" + parts.query if parts.query else "")
        try:
            connection.request(method, path, body=body, headers={"User-Agent": USER_AGENT, **(headers or {})})
            response = connection.getresponse()
            response_headers = {k.lower(): v for k, v in response.getheaders()}
            self.log.append(f"{method} {path} -> {response.status}")
            if response.status in (301, 302, 303, 307, 308) and method in ("GET", "HEAD") and redirects > 0:
                response.read()
                return self.request(method, urllib.parse.urljoin(url, response_headers.get("location", "")), {k: v for k, v in (headers or {}).items() if k.lower() != "authorization"}, None, sink, redirects - 1)
            if sink is not None and response.status == 200:
                for chunk in iter(lambda: response.read(COPY_CHUNK), b""):
                    sink(chunk)
                return response.status, response_headers, b""
            return response.status, response_headers, response.read()
        except OSError as exc:
            raise InputError(f"{url}: {exc}; is the registry running, and does it need --plain-http?")
        finally:
            connection.close()

    def blob_exists(self, repository: str, digest: str) -> bool:
        status, _, body = self.request("HEAD", self.url(repository, f"blobs/{digest}"))
        if status in (200, 404):
            return status == 200
        raise VerifyError(f"HEAD blob {digest} returned {status} {body[:200]!r}")

    def upload(self, repository: str, blob: Blob) -> None:
        status, headers, body = self.request("POST", self.url(repository, "blobs/uploads/"), {"Content-Length": "0"}, b"")
        if status != 202 or "location" not in headers:
            raise VerifyError(f"starting an upload returned {status} {body[:200]!r}; the registry may need credentials or push access")
        location = urllib.parse.urljoin(self.base + "/", headers["location"])
        location += ("&" if "?" in location else "?") + "digest=" + urllib.parse.quote(blob.digest)
        put_headers = {"Content-Type": "application/octet-stream", "Content-Length": str(blob.size)}
        if blob.path is not None:
            with open(blob.path, "rb") as handle:
                status, headers, body = self.request("PUT", location, put_headers, handle)
        else:
            status, headers, body = self.request("PUT", location, put_headers, blob.data)
        if status != 201:
            raise VerifyError(f"uploading {blob.digest} returned {status} {body[:300]!r}")
        returned = headers.get("docker-content-digest")
        if returned and returned != blob.digest:
            raise VerifyError(f"registry stored {returned} for a blob we hashed as {blob.digest}")

    def put_manifest(self, repository: str, reference: str, data: bytes, media_type: str = OCI_MANIFEST) -> str:
        status, headers, body = self.request("PUT", self.url(repository, f"manifests/{reference}"), {"Content-Type": media_type, "Content-Length": str(len(data))}, data)
        if status != 201:
            raise VerifyError(f"PUT manifest returned {status} {body[:300]!r}")
        expected = "sha256:" + hashlib.sha256(data).hexdigest()
        returned = headers.get("docker-content-digest", expected)
        if returned != expected:
            raise VerifyError(f"registry says the manifest is {returned}, we sent {expected}")
        return expected

    def get_manifest(self, repository: str, reference: str) -> Tuple[Dict[str, Any], str, bytes]:
        status, headers, body = self.request("GET", self.url(repository, f"manifests/{reference}"), {"Accept": f"{OCI_MANIFEST}, {DOCKER_MANIFEST}"})
        if status != 200:
            raise VerifyError(f"manifest {repository}:{reference} returned {status} {body[:200]!r}")
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if reference.startswith("sha256:") and digest != reference:
            raise VerifyError(f"manifest bytes hash to {digest}, not the {reference} we asked for")
        return json.loads(body), digest, body

    def small_blob(self, repository: str, digest: str, size: int, limit: int = 4 << 20) -> bytes:
        if size > limit:
            raise VerifyError(f"config blob {digest} claims {size:,} bytes; refusing more than {limit:,}")
        status, _, body = self.request("GET", self.url(repository, f"blobs/{digest}"))
        if status != 200:
            raise VerifyError(f"blob {digest} returned {status} {body[:200]!r}")
        if "sha256:" + hashlib.sha256(body).hexdigest() != digest or len(body) != size:
            raise VerifyError(f"config blob {digest} does not match its descriptor")
        return body

    def download(self, repository: str, digest: str, size: int, target: str) -> None:
        partial = target + ".part"
        hasher = hashlib.sha256()
        written = [0]
        with open(partial, "wb") as out:
            def sink(chunk: bytes) -> None:
                hasher.update(chunk)
                out.write(chunk)
                written[0] += len(chunk)
            status, _, body = self.request("GET", self.url(repository, f"blobs/{digest}"), sink=sink)
        actual = "sha256:" + hasher.hexdigest()
        if status != 200 or actual != digest or written[0] != size:
            os.remove(partial)
            if status != 200:
                raise VerifyError(f"blob {digest} returned {status} {body[:200]!r}")
            raise VerifyError(f"blob {digest}: got {written[0]:,} bytes hashing to {actual}; expected {size:,} bytes; nothing was written")
        os.replace(partial, target)


def push(ref: Ref, files: Sequence[Tuple[str, Optional[str]]], plain_http: bool = False, name: str = "", revision: str = "") -> Dict[str, Any]:
    config, layers, manifest = package(files, name, revision)
    registry = Registry(ref.registry, plain_http)
    uploaded, skipped = [], []
    for blob in [config] + list(layers):
        if registry.blob_exists(ref.repository, blob.digest):
            skipped.append(blob.digest)
        else:
            registry.upload(ref.repository, blob)
            uploaded.append(blob.digest)
    digest = registry.put_manifest(ref.repository, ref.reference, manifest)
    return {
        "reference": str(ref),
        "digest": digest,
        "pinned": f"{ref.registry}/{ref.repository}@{digest}",
        "config": config.digest,
        "layers": [{"file": b.annotations[TITLE], "mediaType": b.media_type, "digest": b.digest, "size": b.size} for b in layers],
        "uploaded": uploaded,
        "skipped": skipped,
        "uploaded_bytes": sum(b.size for b in [config] + list(layers) if b.digest in uploaded),
        "requests": registry.log,
    }


def pull(ref: Ref, dest: str, plain_http: bool = False) -> Dict[str, Any]:
    registry = Registry(ref.registry, plain_http)
    manifest, digest, _ = registry.get_manifest(ref.repository, ref.reference)
    if manifest.get("mediaType", OCI_MANIFEST) != OCI_MANIFEST or "layers" not in manifest:
        raise VerifyError(f"{ref} is not an OCI image manifest (mediaType {manifest.get('mediaType')!r}); an index would need a platform choice")
    os.makedirs(dest, exist_ok=True)
    files = []
    for layer in manifest["layers"]:
        annotations = layer.get("annotations") or {}
        name = annotations.get(FILEPATH) or annotations.get(TITLE)
        if not name:
            continue
        target = safe_join(dest, name)
        registry.download(ref.repository, normalize_digest(layer["digest"]), int(layer["size"]), target)
        files.append({"file": name, "path": target, "digest": layer["digest"], "size": int(layer["size"]), "mediaType": layer.get("mediaType")})
    config_descriptor = manifest.get("config") or {}
    model_config_value = None
    if config_descriptor.get("mediaType") == MODEL_CONFIG:
        model_config_value = json.loads(registry.small_blob(ref.repository, normalize_digest(config_descriptor["digest"]), int(config_descriptor.get("size", 0))))
    return {"reference": str(ref), "digest": digest, "artifactType": manifest.get("artifactType"), "config": config_descriptor.get("digest"), "configMediaType": config_descriptor.get("mediaType"), "modelConfig": model_config_value, "files": files}


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2))


def cmd_inspect(args: argparse.Namespace) -> int:
    model, source = load_model(args.source)
    info = inspection(model, source, model_shape(model, args.config), args.tensors)
    if args.peek:
        info["peek"] = {"tensor": args.peek, "values": peek(model, source, args.peek, args.count)}
    if args.json:
        print_json(info)
    else:
        print(render_inspection(info))
        if args.peek:
            print(f"peek      {args.peek}: " + ", ".join(f"{v:.6g}" for v in info["peek"]["values"]))
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    model: Optional[ModelFile] = None
    config_path = args.config
    if args.source and args.source.endswith(".json"):
        config_path = config_path or args.source
    elif args.source:
        model = load_model(args.source)[0]
    parameters = int(float(args.params)) if args.params else (model.parameters if model else 0)
    if parameters <= 0:
        raise InputError("pass a model file, or --params N with a config.json")
    shape = model_shape(model, config_path)
    formats = args.quant or list(MEMORY_FORMATS)
    file_bytes = (file_type_label(model) or "+".join(model.by_dtype()), model.tensor_bytes) if model else None
    report = memory_report(parameters, shape, formats, args.ctx, args.seqs, args.kv_type, file_bytes, args.quant[0] if args.quant else None, args.gpu_gib, args.utilization)
    if args.json:
        print_json(report)
    else:
        print(render_memory(report, os.path.basename(args.source) if args.source else f"{param_size_label(parameters)} parameters"))
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    expected = normalize_digest(args.expect) if args.expect else None
    actual, size = file_digest(args.file)
    ok = expected is None or expected == actual
    if args.json:
        print_json({"file": args.file, "digest": actual, "size": size, "expected": expected, "ok": ok})
    else:
        print(f"{actual}  {size:,} bytes  {args.file}")
        if expected:
            print("OK: matches the pinned digest" if ok else f"MISMATCH: expected {expected}")
    return 0 if ok else 1


def cmd_fetch(args: argparse.Namespace) -> int:
    result = fetch(args.repo, args.filename, args.revision, args.dest, args.sha256, args.endpoint, log=lambda line: print(line, file=sys.stderr))
    if args.json:
        print_json(result)
    else:
        print(f"{result['digest']}  {result['size']:,} bytes  {result['path']}")
        print(f"commit {result['commit']}; verified against: {', '.join(result['verified_against']) or 'nothing (no digest known)'}")
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    ref = parse_ref(args.ref)
    files = [split_file_arg(item) for item in args.files]
    if args.dry_run or args.config_out:
        config, layers, manifest = package(files, args.name, args.revision)
        if args.config_out:
            with open(args.config_out, "wb") as handle:
                handle.write(config.data or b"")
        if args.dry_run:
            print_json({"reference": str(ref), "digest": "sha256:" + hashlib.sha256(manifest).hexdigest(), "manifest": json.loads(manifest), "config": json.loads(config.data or b"{}")})
            return 0
    result = push(ref, files, args.plain_http, args.name, args.revision)
    if args.json:
        print_json(result)
    else:
        for layer in result["layers"]:
            state = "exists" if layer["digest"] in result["skipped"] else "pushed"
            print(f"  {state:<7} {layer['digest']}  {layer['size']:>14,}  {layer['mediaType']}  {layer['file']}")
        print(f"  {'exists' if result['config'] in result['skipped'] else 'pushed':<7} {result['config']}  config {MODEL_CONFIG}")
        print(f"pushed {result['reference']}: {len(result['uploaded'])} blob(s) uploaded ({result['uploaded_bytes']:,} bytes), {len(result['skipped'])} already present")
        print(f"pin it as {result['pinned']}")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    result = pull(parse_ref(args.ref), args.dest, args.plain_http)
    if args.json:
        print_json(result)
    else:
        print(f"{result['reference']} = {result['digest']} ({result['artifactType']})")
        for item in result["files"]:
            print(f"  {item['digest']}  {item['size']:>14,}  {item['path']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="modelinspect", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("inspect", help="parse a .gguf or .safetensors header (file or http(s) URL) and count parameters and bytes")
    p.add_argument("source")
    p.add_argument("--config", help="Hugging Face config.json for layers and heads (safetensors)")
    p.add_argument("--tensors", action="store_true", help="list every tensor")
    p.add_argument("--peek", metavar="TENSOR", help="decode the first values of one tensor (F32, F16, BF16, Q8_0, Q4_0)")
    p.add_argument("--count", type=int, default=8)
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=cmd_inspect)
    p = sub.add_parser("memory", help="weights per format plus KV cache for a context length")
    p.add_argument("source", nargs="?", help=".gguf, .safetensors, URL or config.json")
    p.add_argument("--params", help="parameter count, e.g. 8e9 (default: counted from the file)")
    p.add_argument("--config", help="config.json for layers and heads")
    p.add_argument("--quant", action="append", help="format(s) to show, e.g. Q4_K (default: a standard list, plus the file itself)")
    p.add_argument("--ctx", type=int, default=8192, help="tokens per sequence (default 8192)")
    p.add_argument("--seqs", type=int, default=1, help="concurrent sequences (default 1)")
    p.add_argument("--kv-type", default="f16", help=f"KV cache element type: {', '.join(KV_TYPES)}")
    p.add_argument("--gpu-gib", type=float, help="GPU memory in GiB: how many KV tokens fit after the weights")
    p.add_argument("--utilization", type=float, default=0.9, help="share of GPU memory the server may use (vLLM's --gpu-memory-utilization, default 0.9)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=cmd_memory)
    p = sub.add_parser("digest", help="sha256 of a file, optionally checked against a pinned digest")
    p.add_argument("file")
    p.add_argument("--expect", help="sha256:HEX or HEX")
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=cmd_digest)
    p = sub.add_parser("fetch", help="download one file from a Hugging Face repo at a pinned revision and verify it")
    p.add_argument("repo")
    p.add_argument("filename")
    p.add_argument("--revision", required=True, help="a 40-character commit id (branches move)")
    p.add_argument("--sha256", help="the digest you pinned; checked before and after the download")
    p.add_argument("--dest", default=".")
    p.add_argument("--endpoint", default=os.environ.get("HF_ENDPOINT", HF_ENDPOINT))
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=cmd_fetch)
    p = sub.add_parser("push", help="push files as a ModelPack OCI artifact")
    p.add_argument("ref", help="registry/repository:tag, e.g. localhost:5000/models/smollm2:q8_0")
    p.add_argument("files", nargs="+", help="FILE or FILE:MEDIATYPE, like oras push")
    p.add_argument("--plain-http", action="store_true")
    p.add_argument("--name", default="", help="descriptor.name in the model config")
    p.add_argument("--revision", default="", help="descriptor.revision, e.g. the Hugging Face commit")
    p.add_argument("--dry-run", action="store_true", help="print the manifest and config without contacting the registry")
    p.add_argument("--config-out", help="also write the config blob to this file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=cmd_push)
    p = sub.add_parser("pull", help="pull an artifact and verify every blob")
    p.add_argument("ref")
    p.add_argument("--dest", default=".")
    p.add_argument("--plain-http", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=cmd_pull)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return args.handler(args)
    except InputError as exc:
        print(f"modelinspect: {exc}", file=sys.stderr)
        return 2
    except VerifyError as exc:
        print(f"modelinspect: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
