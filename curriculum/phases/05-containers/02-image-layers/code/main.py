"""layer_audit: read a `docker save` tarball and explain where every byte of an image comes from."""
from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
import tarfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

WHITEOUT_PREFIX = ".wh."
OPAQUE_WHITEOUT = ".wh..wh..opq"
NOP_PREFIX = "/bin/sh -c #(nop) "
SHELL_PREFIX = "/bin/sh -c "


@dataclass
class Layer:
    ref: str
    size: int = 0
    files: Dict[str, int] = field(default_factory=dict)
    whiteouts: List[str] = field(default_factory=list)
    created_by: str = ""


@dataclass
class ImageReport:
    tags: List[str]
    layers: List[Layer]

    @property
    def total(self) -> int:
        return sum(layer.size for layer in self.layers)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def normalize(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name.lstrip("/")


def open_layer(data: bytes) -> tarfile.TarFile:
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:")


def parse_layer(ref: str, data: bytes) -> Layer:
    layer = Layer(ref=ref)
    with open_layer(data) as tar:
        for member in tar.getmembers():
            name = normalize(member.name)
            directory, _, base = name.rpartition("/")
            if base == OPAQUE_WHITEOUT:
                layer.whiteouts.append(f"{directory}/*" if directory else "*")
                continue
            if base.startswith(WHITEOUT_PREFIX):
                target = base[len(WHITEOUT_PREFIX):]
                layer.whiteouts.append(f"{directory}/{target}" if directory else target)
                continue
            if member.isfile():
                layer.files[name] = member.size
                layer.size += member.size
    return layer


def history_instructions(config: dict) -> List[str]:
    instructions = []
    for entry in config.get("history", []):
        if entry.get("empty_layer"):
            continue
        text = entry.get("created_by", "")
        if text.startswith(NOP_PREFIX):
            text = text[len(NOP_PREFIX):]
        elif text.startswith(SHELL_PREFIX):
            text = "RUN " + text[len(SHELL_PREFIX):]
        instructions.append(" ".join(text.split()))
    return instructions


def read_member(tar: tarfile.TarFile, name: str) -> bytes:
    handle = tar.extractfile(tar.getmember(name))
    if handle is None:
        raise ValueError(f"{name} is not a regular file inside the archive")
    return handle.read()


def load_image(path: str) -> ImageReport:
    with tarfile.open(path, mode="r:*") as tar:
        manifest = json.loads(read_member(tar, "manifest.json"))[0]
        config = json.loads(read_member(tar, manifest["Config"]))
        layers = [parse_layer(ref, read_member(tar, ref)) for ref in manifest["Layers"]]
    for layer, instruction in zip(layers, history_instructions(config)):
        layer.created_by = instruction
    return ImageReport(tags=list(manifest.get("RepoTags") or []), layers=layers)


def deleted_files(layers: Sequence[Layer]) -> List[Tuple[str, int, int]]:
    seen: Dict[str, int] = {}
    deleted: List[Tuple[str, int, int]] = []
    for index, layer in enumerate(layers):
        for target in layer.whiteouts:
            if target.endswith("*"):
                prefix = target[:-1]
                for path in [p for p in seen if p.startswith(prefix)]:
                    deleted.append((path, seen.pop(path), index))
            elif target in seen:
                deleted.append((target, seen.pop(target), index))
        seen.update(layer.files)
    return deleted


def shadowed_files(layers: Sequence[Layer]) -> List[Tuple[str, int, int]]:
    seen: Dict[str, int] = {}
    shadowed: List[Tuple[str, int, int]] = []
    for index, layer in enumerate(layers):
        for path, size in layer.files.items():
            if path in seen:
                shadowed.append((path, seen[path], index))
            seen[path] = size
    return shadowed


def wasted_bytes(layers: Sequence[Layer]) -> int:
    return sum(size for _, size, _ in deleted_files(layers)) + sum(size for _, size, _ in shadowed_files(layers))


def render(report: ImageReport, top: int = 0) -> str:
    lines = [
        f"image: {', '.join(report.tags) or '(untagged)'}",
        f"layers: {len(report.layers)}   total: {human_size(report.total)}",
        "",
    ]
    rows = list(enumerate(report.layers))
    if top:
        rows = sorted(rows, key=lambda row: row[1].size, reverse=True)[:top]
    for index, layer in rows:
        lines.append(f"{index:>2}  {human_size(layer.size):>10}  {layer.created_by or layer.ref}"[:120])
    deleted = deleted_files(report.layers)
    shadowed = shadowed_files(report.layers)
    if deleted or shadowed:
        lines.append("")
        lines.append(f"wasted: {human_size(wasted_bytes(report.layers))} shipped in the image but invisible in the final filesystem")
        for path, size, index in deleted[:10]:
            lines.append(f"  deleted in layer {index}: {path} ({human_size(size)})")
        for path, size, index in shadowed[:10]:
            lines.append(f"  overwritten in layer {index}: {path} ({human_size(size)})")
    return "\n".join(lines)


def to_json(report: ImageReport) -> dict:
    return {
        "tags": report.tags,
        "total": report.total,
        "wasted": wasted_bytes(report.layers),
        "layers": [
            {"index": i, "size": layer.size, "files": len(layer.files), "created_by": layer.created_by, "whiteouts": layer.whiteouts}
            for i, layer in enumerate(report.layers)
        ],
        "deleted": [{"path": p, "size": s, "layer": i} for p, s, i in deleted_files(report.layers)],
        "overwritten": [{"path": p, "size": s, "layer": i} for p, s, i in shadowed_files(report.layers)],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="layer_audit", description=__doc__)
    parser.add_argument("image_tar", help="archive produced by `docker save IMAGE -o file.tar`")
    parser.add_argument("--top", type=int, default=0, help="show only the N largest layers")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    report = load_image(args.image_tar)
    print(json.dumps(to_json(report), indent=2) if args.json else render(report, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
