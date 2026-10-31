"""registry_pull: fetch an image from an OCI registry with plain HTTP, verify every digest, and unpack the layers into a root filesystem."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

DEFAULT_REGISTRY = "registry-1.docker.io"
MANIFEST_TYPES = [
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
]
INDEX_TYPES = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}

Response = Tuple[int, Dict[str, str], bytes]
Http = Callable[[str, Dict[str, str]], Response]


@dataclass(frozen=True)
class Reference:
    registry: str
    repository: str
    tag: Optional[str]
    digest: Optional[str]

    @property
    def reference(self) -> str:
        return self.digest or self.tag or "latest"

    def __str__(self) -> str:
        suffix = f"@{self.digest}" if self.digest else f":{self.tag or 'latest'}"
        return f"{self.registry}/{self.repository}{suffix}"


def parse_reference(text: str) -> Reference:
    digest = None
    if "@" in text:
        text, digest = text.split("@", 1)
    first, _, rest = text.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, rest
    else:
        registry, path = DEFAULT_REGISTRY, text
    if registry in ("docker.io", "index.docker.io"):
        registry = DEFAULT_REGISTRY
    tag = None
    if ":" in path.rsplit("/", 1)[-1]:
        path, tag = path.rsplit(":", 1)
    if registry == DEFAULT_REGISTRY and "/" not in path:
        path = f"library/{path}"
    if digest is None and tag is None:
        tag = "latest"
    return Reference(registry, path, tag, digest)


def parse_challenge(header: str) -> Optional[str]:
    if not header or not header.lower().startswith("bearer "):
        return None
    params = dict(re.findall(r'(\w+)="([^"]*)"', header))
    realm = params.get("realm")
    if not realm:
        return None
    query = {k: v for k, v in params.items() if k in ("service", "scope")}
    return realm + ("?" + urllib.parse.urlencode(query) if query else "")


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def verify_digest(data: bytes, digest: str) -> None:
    actual = sha256_digest(data)
    if actual != digest:
        raise ValueError(f"digest mismatch: expected {digest}, got {actual}")


def select_platform(index: dict, os_name: str = "linux", arch: str = "amd64", variant: Optional[str] = None) -> dict:
    for entry in index.get("manifests", []):
        platform = entry.get("platform") or {}
        if platform.get("os") == os_name and platform.get("architecture") == arch and (variant is None or platform.get("variant") in (None, variant)):
            return entry
    available = ", ".join(f"{m.get('platform', {}).get('os')}/{m.get('platform', {}).get('architecture')}" for m in index.get("manifests", []))
    raise LookupError(f"no manifest for {os_name}/{arch}; available: {available}")


def layer_summary(manifest: dict) -> List[Tuple[str, int, str]]:
    return [(layer["digest"], int(layer.get("size", 0)), layer.get("mediaType", "")) for layer in manifest.get("layers", [])]


def urllib_http(url: str, headers: Dict[str, str]) -> Response:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()


class Client:
    def __init__(self, http: Http = urllib_http, log: Callable[[str], None] = lambda line: None) -> None:
        self.http = http
        self.log = log
        self.tokens: Dict[str, str] = {}

    def _get(self, ref: Reference, path: str, accept: Sequence[str]) -> Response:
        url = f"https://{ref.registry}/v2/{ref.repository}/{path}"
        headers = {"Accept": ", ".join(accept)}
        token = self.tokens.get(ref.repository)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        status, resp_headers, body = self.http(url, headers)
        if status == 401 and not token:
            token_url = parse_challenge(resp_headers.get("www-authenticate", ""))
            if not token_url:
                raise PermissionError(f"401 from {url} without a Bearer challenge")
            self.log(f"token ← {token_url}")
            t_status, _, t_body = self.http(token_url, {})
            if t_status != 200:
                raise PermissionError(f"token endpoint returned {t_status}")
            payload = json.loads(t_body)
            self.tokens[ref.repository] = payload.get("token") or payload.get("access_token") or ""
            headers["Authorization"] = f"Bearer {self.tokens[ref.repository]}"
            status, resp_headers, body = self.http(url, headers)
        self.log(f"{status} {url}")
        return status, resp_headers, body

    def fetch_manifest(self, ref: Reference, os_name: str = "linux", arch: str = "amd64") -> Tuple[dict, str]:
        status, headers, body = self._get(ref, f"manifests/{ref.reference}", MANIFEST_TYPES)
        if status != 200:
            raise LookupError(f"manifest {ref} returned {status}: {body[:200]!r}")
        if ref.digest:
            verify_digest(body, ref.digest)
        manifest = json.loads(body)
        digest = headers.get("docker-content-digest") or sha256_digest(body)
        media_type = manifest.get("mediaType") or headers.get("content-type", "")
        if media_type in INDEX_TYPES or "manifests" in manifest:
            entry = select_platform(manifest, os_name, arch)
            return self.fetch_manifest(Reference(ref.registry, ref.repository, None, entry["digest"]), os_name, arch)
        return manifest, digest

    def fetch_blob(self, ref: Reference, digest: str) -> bytes:
        status, _, body = self._get(ref, f"blobs/{digest}", ["*/*"])
        if status != 200:
            raise LookupError(f"blob {digest} returned {status}")
        verify_digest(body, digest)
        return body

    def pull(self, ref: Reference, dest: str, os_name: str = "linux", arch: str = "amd64") -> dict:
        manifest, digest = self.fetch_manifest(ref, os_name, arch)
        os.makedirs(os.path.join(dest, "blobs"), exist_ok=True)
        config = self.fetch_blob(ref, manifest["config"]["digest"])
        with open(os.path.join(dest, "blobs", manifest["config"]["digest"].replace(":", "_")), "wb") as handle:
            handle.write(config)
        layers = []
        for layer_digest, size, media_type in layer_summary(manifest):
            data = self.fetch_blob(ref, layer_digest)
            path = os.path.join(dest, "blobs", layer_digest.replace(":", "_"))
            with open(path, "wb") as handle:
                handle.write(data)
            layers.append({"digest": layer_digest, "size": len(data), "mediaType": media_type, "path": path})
        with open(os.path.join(dest, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
        return {"reference": str(ref), "digest": digest, "config": manifest["config"]["digest"], "layers": layers}


def _safe_join(root: str, name: str) -> str:
    target = os.path.normpath(os.path.join(root, name))
    if not target.startswith(os.path.abspath(root)):
        raise ValueError(f"layer entry escapes rootfs: {name}")
    return target


def apply_layer(data: bytes, rootfs: str) -> Tuple[int, int]:
    import gzip
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    added = removed = 0
    rootfs = os.path.abspath(rootfs)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for member in tar.getmembers():
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            name = name.lstrip("/")
            directory, _, base = name.rpartition("/")
            if base == ".wh..wh..opq":
                target = _safe_join(rootfs, directory)
                if os.path.isdir(target):
                    for child in os.listdir(target):
                        child_path = os.path.join(target, child)
                        if os.path.isdir(child_path) and not os.path.islink(child_path):
                            import shutil
                            shutil.rmtree(child_path)
                        else:
                            os.remove(child_path)
                        removed += 1
                continue
            if base.startswith(".wh."):
                target = _safe_join(rootfs, f"{directory}/{base[4:]}" if directory else base[4:])
                if os.path.isdir(target) and not os.path.islink(target):
                    import shutil
                    shutil.rmtree(target)
                    removed += 1
                elif os.path.lexists(target):
                    os.remove(target)
                    removed += 1
                continue
            if member.isdev():
                continue
            _safe_join(rootfs, name)
            tar.extract(member, rootfs, set_attrs=False)
            added += 1
    return added, removed


def extract_rootfs(layer_paths: Sequence[str], rootfs: str) -> Tuple[int, int]:
    total_added = total_removed = 0
    os.makedirs(rootfs, exist_ok=True)
    for path in layer_paths:
        with open(path, "rb") as handle:
            added, removed = apply_layer(handle.read(), rootfs)
        total_added += added
        total_removed += removed
    return total_added, total_removed


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="registry_pull", description=__doc__)
    parser.add_argument("command", choices=["manifest", "pull", "rootfs"])
    parser.add_argument("image", help="e.g. alpine:3.22, ghcr.io/org/app@sha256:…")
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--dest", default="./image")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    os_name, _, arch = args.platform.partition("/")
    ref = parse_reference(args.image)
    log = (lambda line: print(f"[registry_pull] {line}", file=sys.stderr)) if args.verbose else (lambda line: None)
    client = Client(log=log)

    if args.command == "manifest":
        manifest, digest = client.fetch_manifest(ref, os_name, arch or "amd64")
        if args.json:
            print(json.dumps({"reference": str(ref), "digest": digest, "manifest": manifest}, indent=2))
        else:
            print(f"{ref}\ndigest: {digest}\nconfig: {manifest['config']['digest']}")
            for layer_digest, size, media_type in layer_summary(manifest):
                print(f"  {layer_digest}  {size:>10} B  {media_type}")
        return 0

    result = client.pull(ref, args.dest, os_name, arch or "amd64")
    if args.command == "rootfs":
        rootfs = os.path.join(args.dest, "rootfs")
        added, removed = extract_rootfs([layer["path"] for layer in result["layers"]], rootfs)
        result["rootfs"] = {"path": rootfs, "files_added": added, "entries_removed": removed}
    print(json.dumps(result, indent=2) if args.json else f"pulled {result['reference']} ({len(result['layers'])} layers) into {args.dest}" + (f"\nrootfs at {result['rootfs']['path']}: {result['rootfs']['files_added']} files" if "rootfs" in result else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
