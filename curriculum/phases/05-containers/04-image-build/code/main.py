"""mkimage: build an OCI image from plain directories, with no Dockerfile and no daemon, packaged so that docker load, skopeo and registries all accept it."""
from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import io
import json
import os
import platform
import re
import stat
import sys
import tarfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
CONFIG_TYPE = "application/vnd.oci.image.config.v1+json"
LAYER_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"
TAG_RE = re.compile(r"^(?P<name>[a-z0-9][a-z0-9._/-]*):(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})$")
ARCHES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "arm"}


@dataclass
class Layer:
    source: str
    tar: bytes
    gz: bytes
    diff_id: str
    digest: str
    created_by: str

    @property
    def size(self) -> int:
        return len(self.gz)


@dataclass
class Image:
    tag: str
    layers: List[Layer]
    config: bytes
    manifest: bytes
    index: bytes

    @property
    def image_id(self) -> str:
        return sha256_digest(self.config)

    @property
    def manifest_digest(self) -> str:
        return sha256_digest(self.manifest)


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def verify_digest(data: bytes, digest: str) -> None:
    actual = sha256_digest(data)
    if actual != digest:
        raise ValueError(f"digest mismatch: expected {digest}, got {actual}")


def blob_path(digest: str) -> str:
    algorithm, _, hex_digest = digest.partition(":")
    return f"blobs/{algorithm}/{hex_digest}"


def host_arch() -> str:
    return ARCHES.get(platform.machine().lower(), platform.machine().lower() or "amd64")


def rfc3339(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def chain_ids(diff_ids: Sequence[str]) -> List[str]:
    chain: List[str] = []
    for diff_id in diff_ids:
        chain.append(diff_id if not chain else sha256_digest(f"{chain[-1]} {diff_id}".encode()))
    return chain


def walk_sorted(root: str) -> List[str]:
    out: List[str] = []

    def visit(rel: str) -> None:
        full = os.path.join(root, rel) if rel else root
        for name in sorted(os.listdir(full)):
            child_rel = f"{rel}/{name}" if rel else name
            out.append(child_rel)
            child = os.path.join(root, child_rel)
            if os.path.isdir(child) and not os.path.islink(child):
                visit(child_rel)

    visit("")
    return out


def normalise(info: tarfile.TarInfo, epoch: int, keep_owners: bool) -> tarfile.TarInfo:
    info.mtime = epoch
    info.pax_headers = {}
    if not keep_owners:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
    return info


def layer_tar(root: str, epoch: int = 0, keep_owners: bool = False, deletes: Sequence[str] = ()) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in walk_sorted(root):
            full = os.path.join(root, rel)
            if stat.S_ISSOCK(os.lstat(full).st_mode):
                continue
            info = tar.gettarinfo(full, arcname=rel)
            if info is None or info.isdev():
                continue
            normalise(info, epoch, keep_owners)
            if info.isreg():
                with open(full, "rb") as handle:
                    tar.addfile(info, handle)
            else:
                tar.addfile(info)
        for path in sorted(deletes):
            directory, _, base = path.strip("/").rpartition("/")
            info = tarfile.TarInfo(f"{directory}/.wh.{base}" if directory else f".wh.{base}")
            tar.addfile(normalise(info, epoch, False))
    return buffer.getvalue()


def gzip_bytes(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(data)
    return buffer.getvalue()


def build_layer(source: str, epoch: int = 0, keep_owners: bool = False, deletes: Sequence[str] = (), created_by: Optional[str] = None) -> Layer:
    raw = layer_tar(source, epoch, keep_owners, deletes)
    gz = gzip_bytes(raw)
    label = created_by or f"mkimage --layer {os.path.basename(os.path.abspath(source))}" + "".join(f" --delete {d}" for d in sorted(deletes))
    return Layer(source, raw, gz, sha256_digest(raw), sha256_digest(gz), label)


def canonical_json(obj: object) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode()


def image_config(layers: Sequence[Layer], arch: str, os_name: str, epoch: int, entrypoint: Sequence[str] = (), cmd: Sequence[str] = (), env: Sequence[str] = (), workdir: str = "", labels: Optional[Dict[str, str]] = None, user: str = "") -> bytes:
    runtime: Dict[str, object] = {}
    if entrypoint:
        runtime["Entrypoint"] = list(entrypoint)
    if cmd:
        runtime["Cmd"] = list(cmd)
    if env:
        runtime["Env"] = list(env)
    if workdir:
        runtime["WorkingDir"] = workdir
    if labels:
        runtime["Labels"] = dict(labels)
    if user:
        runtime["User"] = user
    config = {
        "architecture": arch,
        "os": os_name,
        "created": rfc3339(epoch),
        "config": runtime,
        "rootfs": {"type": "layers", "diff_ids": [layer.diff_id for layer in layers]},
        "history": [{"created": rfc3339(epoch), "created_by": layer.created_by} for layer in layers],
    }
    return canonical_json(config)


def image_manifest(config: bytes, layers: Sequence[Layer]) -> bytes:
    manifest = {
        "schemaVersion": 2,
        "mediaType": MANIFEST_TYPE,
        "config": {"mediaType": CONFIG_TYPE, "digest": sha256_digest(config), "size": len(config)},
        "layers": [{"mediaType": LAYER_TYPE, "digest": layer.digest, "size": layer.size} for layer in layers],
    }
    return canonical_json(manifest)


def image_index(manifest: bytes, tag: str) -> bytes:
    name, _, version = tag.partition(":")
    full_name = name if "/" in name and ("." in name.split("/")[0] or ":" in name.split("/")[0]) else f"docker.io/{'library/' if '/' not in name else ''}{name}"
    index = {
        "schemaVersion": 2,
        "mediaType": INDEX_TYPE,
        "manifests": [
            {
                "mediaType": MANIFEST_TYPE,
                "digest": sha256_digest(manifest),
                "size": len(manifest),
                "annotations": {"io.containerd.image.name": f"{full_name}:{version}", "org.opencontainers.image.ref.name": version},
            }
        ],
    }
    return canonical_json(index)


def parse_pairs(items: Sequence[str], what: str) -> Dict[str, str]:
    pairs: Dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"{what} must look like KEY=value, got {item!r}")
        pairs[key] = value
    return pairs


def build(sources: Sequence[str], tag: str, epoch: int = 0, arch: Optional[str] = None, os_name: str = "linux", entrypoint: Sequence[str] = (), cmd: Sequence[str] = (), env: Sequence[str] = (), workdir: str = "", labels: Optional[Dict[str, str]] = None, user: str = "", keep_owners: bool = False, deletes: Sequence[str] = ()) -> Image:
    if not TAG_RE.match(tag):
        raise ValueError(f"tag must look like name:tag (lowercase name), got {tag!r}")
    if not sources:
        raise ValueError("at least one --layer DIR is required")
    for source in sources:
        if not os.path.isdir(source):
            raise ValueError(f"layer source is not a directory: {source}")
    layers = [build_layer(source, epoch, keep_owners, deletes if index == len(sources) - 1 else ()) for index, source in enumerate(sources)]
    config = image_config(layers, arch or host_arch(), os_name, epoch, entrypoint, cmd, env, workdir, labels, user)
    manifest = image_manifest(config, layers)
    return Image(tag, layers, config, manifest, image_index(manifest, tag))


def archive_members(image: Image) -> List[tuple]:
    legacy = [{"Config": blob_path(image.image_id), "RepoTags": [image.tag], "Layers": [blob_path(layer.digest) for layer in image.layers]}]
    members = [
        ("oci-layout", canonical_json({"imageLayoutVersion": "1.0.0"})),
        ("index.json", image.index),
        (blob_path(image.manifest_digest), image.manifest),
        (blob_path(image.image_id), image.config),
    ]
    members += [(blob_path(layer.digest), layer.gz) for layer in image.layers]
    members.append(("manifest.json", canonical_json(legacy)))
    return members


def write_archive(image: Image, path: str, epoch: int = 0) -> int:
    with tarfile.open(path, mode="w", format=tarfile.PAX_FORMAT) as tar:
        seen_dirs = set()
        for name, data in archive_members(image):
            parent = os.path.dirname(name)
            parts = parent.split("/") if parent else []
            for depth in range(1, len(parts) + 1):
                directory = "/".join(parts[:depth])
                if directory not in seen_dirs:
                    seen_dirs.add(directory)
                    info = tarfile.TarInfo(directory)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tar.addfile(normalise(info, epoch, False))
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(normalise(info, epoch, False), io.BytesIO(data))
    return os.path.getsize(path)


def write_oci_dir(image: Image, directory: str) -> List[str]:
    written = []
    for name, data in archive_members(image):
        if name == "manifest.json":
            continue
        target = os.path.join(directory, name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(data)
        written.append(name)
    return written


def summary(image: Image) -> dict:
    chains = chain_ids([layer.diff_id for layer in image.layers])
    return {
        "tag": image.tag,
        "image_id": image.image_id,
        "manifest_digest": image.manifest_digest,
        "config": json.loads(image.config),
        "layers": [
            {"source": layer.source, "size": layer.size, "uncompressed": len(layer.tar), "diff_id": layer.diff_id, "digest": layer.digest, "chain_id": chains[i], "created_by": layer.created_by}
            for i, layer in enumerate(image.layers)
        ],
    }


def verify_archive(path: str) -> dict:
    with tarfile.open(path, mode="r:*") as tar:
        members = {member.name: member for member in tar.getmembers()}

        def read(name: str) -> bytes:
            if name not in members:
                raise ValueError(f"archive is missing {name}")
            handle = tar.extractfile(members[name])
            return handle.read() if handle else b""

        index = json.loads(read("index.json"))
        descriptor = index["manifests"][0]
        manifest_bytes = read(blob_path(descriptor["digest"]))
        verify_digest(manifest_bytes, descriptor["digest"])
        if len(manifest_bytes) != descriptor["size"]:
            raise ValueError("manifest size does not match the index")
        manifest = json.loads(manifest_bytes)
        config_bytes = read(blob_path(manifest["config"]["digest"]))
        verify_digest(config_bytes, manifest["config"]["digest"])
        config = json.loads(config_bytes)
        diff_ids = config["rootfs"]["diff_ids"]
        if len(diff_ids) != len(manifest["layers"]):
            raise ValueError("config lists a different number of layers than the manifest")
        layers = []
        for layer_descriptor, diff_id in zip(manifest["layers"], diff_ids):
            gz = read(blob_path(layer_descriptor["digest"]))
            verify_digest(gz, layer_descriptor["digest"])
            if len(gz) != layer_descriptor["size"]:
                raise ValueError(f"layer {layer_descriptor['digest']} size does not match the manifest")
            raw = gzip.decompress(gz)
            verify_digest(raw, diff_id)
            layers.append({"digest": layer_descriptor["digest"], "size": len(gz), "diff_id": diff_id, "uncompressed": len(raw)})
        legacy = json.loads(read("manifest.json"))[0]
        for name in [legacy["Config"], *legacy["Layers"]]:
            if name not in members:
                raise ValueError(f"manifest.json points at a missing member {name}")
        return {
            "tag": legacy["RepoTags"][0],
            "image_id": manifest["config"]["digest"],
            "manifest_digest": descriptor["digest"],
            "layers": layers,
            "chain_ids": chain_ids(diff_ids),
            "config": config,
        }


def render(info: dict) -> str:
    lines = [f"image:    {info['tag']}", f"image id: {info['image_id']}  (sha256 of the config; docker's image ID on the classic store)", f"manifest: {info['manifest_digest']}  (what a registry pulls by; docker's image ID on the containerd store)"]
    for index, layer in enumerate(info["layers"]):
        source = layer.get("source", "")
        lines.append(f"layer {index}: {layer['size']:>9} B gzip  {layer['uncompressed']:>10} B tar  diff_id {layer['diff_id'][:19]}…  digest {layer['digest'][:19]}…  {source}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mkimage", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build an image from one or more directories")
    b.add_argument("--layer", action="append", default=[], metavar="DIR", help="directory that becomes one layer (repeat, in order)")
    b.add_argument("--tag", required=True, help="name:tag")
    b.add_argument("--entrypoint", action="append", default=[], metavar="ARG", help="repeat per argument")
    b.add_argument("--cmd", action="append", default=[], metavar="ARG", help="repeat per argument (use --cmd=-c for values that start with a dash)")
    b.add_argument("--env", action="append", default=[], metavar="KEY=value")
    b.add_argument("--label", action="append", default=[], metavar="KEY=value")
    b.add_argument("--workdir", default="")
    b.add_argument("--user", default="")
    b.add_argument("--arch", help=f"default: this machine ({host_arch()})")
    b.add_argument("--os", default="linux")
    b.add_argument("--epoch", type=int, default=int(os.environ.get("SOURCE_DATE_EPOCH", "0")), help="timestamp for every file and the config (default $SOURCE_DATE_EPOCH or 0)")
    b.add_argument("--keep-owners", action="store_true", help="keep uid/gid from disk instead of root")
    b.add_argument("--delete", action="append", default=[], metavar="PATH", help="whiteout PATH from lower layers (added to the last layer)")
    b.add_argument("--out", required=True, help="archive for docker load / podman load")
    b.add_argument("--oci-dir", help="also write an OCI image layout directory for skopeo / crane")
    b.add_argument("--json", action="store_true")
    v = sub.add_parser("verify", help="re-derive every digest in an archive")
    v.add_argument("archive")
    v.add_argument("--json", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "build":
            image = build(args.layer, args.tag, args.epoch, args.arch, args.os, args.entrypoint, args.cmd, args.env, args.workdir, parse_pairs(args.label, "--label"), args.user, args.keep_owners, args.delete)
            size = write_archive(image, args.out, args.epoch)
            info = summary(image)
            info["archive"] = {"path": args.out, "size": size}
            if args.oci_dir:
                info["oci_dir"] = {"path": args.oci_dir, "files": write_oci_dir(image, args.oci_dir)}
            print(json.dumps(info, indent=2) if args.json else render(info) + f"\nwrote {args.out} ({size} bytes)" + (f" and OCI layout {args.oci_dir}" if args.oci_dir else ""))
        else:
            info = verify_archive(args.archive)
            print(json.dumps(info, indent=2) if args.json else render(info) + "\nevery digest verified")
    except (ValueError, OSError, KeyError) as exc:
        print(f"mkimage: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
