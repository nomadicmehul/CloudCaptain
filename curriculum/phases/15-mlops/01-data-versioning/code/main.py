from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

CHUNK = 1 << 20
DIR_SUFFIX = ".dir"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
DEFAULT_LINKS = ("reflink", "copy")
FICLONE = 0x40049409
READ_ONLY = 0o444
WRITABLE = 0o644
DVC_GITIGNORE = "/config.local\n/tmp\n/cache\n"
SKIP_DIRS = {".git", ".dvc"}
PLAIN_SCALAR = re.compile(r"^[A-Za-z0-9_./][A-Za-z0-9_./@+=,-]*( [A-Za-z0-9_./@+=,-]+)*$")
KEY_LINE = re.compile(r"""^('[^']*'|"[^"]*"|[^\s'"#-][^:]*?|-[^\s:][^:]*?):(?:\s+(.*))?$""")
SECTION = re.compile(r"""^\[\s*'?(.*?)'?\s*\]$""")


class MiniDvcError(Exception):
    code = 1


class UsageError(MiniDvcError):
    code = 2


@dataclass(frozen=True)
class HashInfo:
    md5: str
    size: int
    nfiles: Optional[int] = None

    @property
    def is_dir(self) -> bool:
        return self.md5.endswith(DIR_SUFFIX)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"md5": self.md5, "size": self.size}
        if self.is_dir:
            data["nfiles"] = self.nfiles
        return data


@dataclass(frozen=True)
class DirEntry:
    relpath: str
    md5: str
    size: int


@dataclass
class Out:
    path: str
    info: HashInfo
    source: str


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def walk_files(root: Path) -> List[Tuple[str, Path]]:
    found: List[Tuple[str, Path]] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in files:
            full = Path(current) / name
            found.append((full.relative_to(root).as_posix(), full))
    return sorted(found)


def manifest_bytes(entries: Iterable[DirEntry]) -> bytes:
    rows = sorted(({"md5": e.md5, "relpath": e.relpath} for e in entries), key=lambda row: row["relpath"])
    return json.dumps(rows, sort_keys=True).encode("utf-8")


def parse_manifest(data: bytes) -> Dict[str, str]:
    try:
        rows = json.loads(data.decode("utf-8"))
        return {row["relpath"]: row["md5"] for row in rows}
    except (ValueError, KeyError, TypeError) as exc:
        raise MiniDvcError(f"corrupt .dir manifest: {exc}") from exc


def hash_tree(root: Path) -> Tuple[HashInfo, List[DirEntry], bytes]:
    entries = [DirEntry(rel, file_md5(full), full.stat().st_size) for rel, full in walk_files(root)]
    data = manifest_bytes(entries)
    info = HashInfo(hashlib.md5(data).hexdigest() + DIR_SUFFIX, sum(e.size for e in entries), len(entries))
    return info, entries, data


def hash_path(path: Path) -> Tuple[HashInfo, List[DirEntry], bytes]:
    if path.is_dir():
        return hash_tree(path)
    if path.is_file():
        return HashInfo(file_md5(path), path.stat().st_size), [], b""
    raise MiniDvcError(f"{path}: no such file or directory")


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    reserved = text.lower() in {"true", "false", "null", "yes", "no", "on", "off", "~"}
    numeric = re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", text) is not None
    if PLAIN_SCALAR.match(text) and not reserved and not numeric:
        return text
    if "'" not in text and "\n" not in text:
        return "'" + text + "'"
    return json.dumps(text)


def dump_yaml(value: Any, indent: int = 0) -> List[str]:
    pad = " " * indent
    lines: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict) and item:
                lines.append(f"{pad}{key}:")
                lines.extend(dump_yaml(item, indent + 2))
            elif isinstance(item, list) and item:
                lines.append(f"{pad}{key}:")
                lines.extend(dump_yaml(item, indent))
            elif isinstance(item, (dict, list)):
                lines.append(f"{pad}{key}: {'{}' if isinstance(item, dict) else '[]'}")
            else:
                lines.append(f"{pad}{key}: {yaml_scalar(item)}")
        return lines
    for item in value:
        if isinstance(item, (dict, list)) and item:
            inner = dump_yaml(item, indent + 2)
            lines.append(pad + "- " + inner[0][indent + 2:])
            lines.extend(inner[1:])
        else:
            lines.append(f"{pad}- {yaml_scalar(item)}")
    return lines


def to_yaml(value: Any) -> str:
    return "\n".join(dump_yaml(value)) + "\n"


def strip_comment(text: str) -> str:
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
    return text.rstrip()


def parse_scalar(text: str) -> Any:
    text = text.strip()
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        try:
            return json.loads(text)
        except ValueError as exc:
            raise UsageError(f"YAML: cannot read double-quoted scalar {text!r}") from exc
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if text == "[]":
        return []
    if text == "{}":
        return {}
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    return text


def is_sequence_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def load_yaml(text: str) -> Any:
    lines: List[List[Any]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or raw.strip() == "---":
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise UsageError("YAML: tabs are not allowed for indentation")
        lines.append([len(raw) - len(raw.lstrip(" ")), raw.strip()])
    if not lines:
        return None
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise UsageError(f"YAML: unexpected indentation at {lines[index][1]!r}")
    return value


def _parse_block(lines: List[List[Any]], index: int, indent: int) -> Tuple[Any, int]:
    if is_sequence_item(lines[index][1]):
        return _parse_sequence(lines, index, indent)
    if KEY_LINE.match(lines[index][1]):
        return _parse_mapping(lines, index, indent)
    return _parse_value(lines[index][1], lines, index, indent - 1)


def _parse_value(first: str, lines: List[List[Any]], index: int, indent: int) -> Tuple[Any, int]:
    text = first
    index += 1
    quote = text[0] if text[:1] in ("'", '"') else ""
    while index < len(lines) and lines[index][0] > indent:
        if quote and len(text) > 1 and text.endswith(quote) and not text.endswith("\\" + quote):
            break
        if not quote and (is_sequence_item(lines[index][1]) or KEY_LINE.match(lines[index][1])):
            break
        text += " " + lines[index][1]
        index += 1
    return parse_scalar(text if quote else strip_comment(text)), index


def _parse_sequence(lines: List[List[Any]], index: int, indent: int) -> Tuple[List[Any], int]:
    items: List[Any] = []
    while index < len(lines) and lines[index][0] == indent and is_sequence_item(lines[index][1]):
        rest = lines[index][1][1:].lstrip()
        if not rest:
            if index + 1 < len(lines) and lines[index + 1][0] > indent:
                value, index = _parse_block(lines, index + 1, lines[index + 1][0])
            else:
                value, index = None, index + 1
        elif KEY_LINE.match(rest) or is_sequence_item(rest):
            item_indent = indent + len(lines[index][1]) - len(rest)
            lines[index] = [item_indent, rest]
            value, index = _parse_block(lines, index, item_indent)
        else:
            value, index = _parse_value(rest, lines, index, indent)
        items.append(value)
    return items, index


def _parse_mapping(lines: List[List[Any]], index: int, indent: int) -> Tuple[Dict[str, Any], int]:
    mapping: Dict[str, Any] = {}
    while index < len(lines) and lines[index][0] == indent and not is_sequence_item(lines[index][1]):
        match = KEY_LINE.match(lines[index][1])
        if not match:
            raise UsageError(f"YAML: expected 'key: value', got {lines[index][1]!r}")
        key = parse_scalar(match.group(1))
        rest = strip_comment(match.group(2) or "")
        if rest:
            value, index = _parse_value(match.group(2), lines, index, indent)
        elif index + 1 < len(lines) and (
            lines[index + 1][0] > indent or (lines[index + 1][0] == indent and is_sequence_item(lines[index + 1][1]))
        ):
            value, index = _parse_block(lines, index + 1, lines[index + 1][0])
        else:
            value, index = None, index + 1
        mapping[str(key)] = value
    return mapping, index


def pointer_text(info: HashInfo, name: str) -> str:
    out: Dict[str, Any] = {"md5": info.md5, "size": info.size}
    if info.is_dir:
        out["nfiles"] = info.nfiles
    out["hash"] = "md5"
    out["path"] = name
    return to_yaml({"outs": [out]})


def info_from_entry(entry: Dict[str, Any], where: str) -> HashInfo:
    md5 = entry.get("md5")
    if not isinstance(md5, str) or not re.fullmatch(r"[0-9a-f]{32}(\.dir)?", md5):
        raise MiniDvcError(f"{where}: entry {entry.get('path')!r} has no valid md5")
    if entry.get("hash", "md5") != "md5":
        raise MiniDvcError(f"{where}: hash {entry.get('hash')!r} is not supported (DVC 2 files use md5-dos2unix; run `dvc cache migrate`)")
    return HashInfo(md5, int(entry.get("size") or 0), entry.get("nfiles"))


def read_config(path: Path) -> Dict[str, Dict[str, str]]:
    config: Dict[str, Dict[str, str]] = {}
    section: Optional[Dict[str, str]] = None
    if not path.exists():
        return config
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        header = SECTION.match(line)
        if header:
            section = config.setdefault(header.group(1).strip(), {})
            continue
        if section is None or "=" not in line:
            raise UsageError(f"{path}:{number}: cannot read {raw!r}")
        key, value = line.split("=", 1)
        section[key.strip()] = value.strip()
    return config


def write_config(path: Path, config: Dict[str, Dict[str, str]]) -> None:
    lines: List[str] = []
    for name, values in config.items():
        lines.append(f"['{name}']" if " " in name else f"[{name}]")
        lines.extend(f"    {key} = {value}" for key, value in values.items())
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def object_key(md5: str) -> str:
    return f"files/md5/{md5[:2]}/{md5[2:]}"


def key_to_md5(key: str) -> Optional[str]:
    parts = key.split("/")
    if len(parts) < 4 or parts[-4:-2] != ["files", "md5"]:
        return None
    md5 = parts[-2] + parts[-1]
    return md5 if re.fullmatch(r"[0-9a-f]{32}(\.dir)?", md5) else None


class Cache:
    def __init__(self, root: Path):
        self.root = root

    def path(self, md5: str) -> Path:
        return self.root / object_key(md5)

    def has(self, md5: str) -> bool:
        return self.path(md5).is_file()

    def put_file(self, source: Path, md5: str) -> bool:
        if self.has(md5):
            return False
        target = self.path(md5)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + f".tmp{os.getpid()}")
        shutil.copyfile(source, temp)
        os.chmod(temp, READ_ONLY)
        os.replace(temp, target)
        return True

    def put_bytes(self, data: bytes, md5: str) -> bool:
        if self.has(md5):
            return False
        target = self.path(md5)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + f".tmp{os.getpid()}")
        temp.write_bytes(data)
        os.chmod(temp, READ_ONLY)
        os.replace(temp, target)
        return True

    def manifest(self, md5: str) -> Optional[Dict[str, str]]:
        return parse_manifest(self.path(md5).read_bytes()) if self.has(md5) else None

    def objects(self) -> Set[str]:
        base = self.root / "files" / "md5"
        if not base.is_dir():
            return set()
        return {prefix.name + item.name for prefix in base.iterdir() if prefix.is_dir() for item in prefix.iterdir() if item.is_file()}

    def remove(self, md5: str) -> None:
        target = self.path(md5)
        os.chmod(target, WRITABLE)
        target.unlink()


def reflink(source: Path, target: Path) -> None:
    system = platform.system()
    if system == "Linux":
        import fcntl

        with open(source, "rb") as src, open(target, "wb") as dst:
            fcntl.ioctl(dst.fileno(), FICLONE, src.fileno())
        return
    if system == "Darwin":
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        if libc.clonefile(os.fsencode(str(source)), os.fsencode(str(target)), 0) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(target))
        return
    raise OSError(errno.EOPNOTSUPP, f"reflink is not supported on {system}")


def copy(source: Path, target: Path) -> None:
    shutil.copyfile(source, target)


def hardlink(source: Path, target: Path) -> None:
    os.link(source, target)


def symlink(source: Path, target: Path) -> None:
    os.symlink(source, target)


LINKERS: Dict[str, Callable[[Path, Path], None]] = {"reflink": reflink, "hardlink": hardlink, "symlink": symlink, "copy": copy}
WRITABLE_AFTER = {"reflink", "copy"}


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def link_file(source: Path, target: Path, strategies: Sequence[str], linkers: Dict[str, Callable[[Path, Path], None]]) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    failures: List[str] = []
    for name in strategies:
        if target.exists() or target.is_symlink():
            remove_path(target)
        try:
            linkers[name](source, target)
        except (OSError, NotImplementedError) as exc:
            failures.append(f"{name}: {exc}")
            if target.exists() or target.is_symlink():
                remove_path(target)
            continue
        if name in WRITABLE_AFTER:
            os.chmod(target, WRITABLE)
        return name
    raise MiniDvcError(f"cannot link {target}: " + "; ".join(failures))


def parse_links(text: str) -> List[str]:
    names = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [name for name in names if name not in LINKERS]
    if not names or unknown:
        raise UsageError(f"unknown link type(s) {unknown or text!r}; choose from {', '.join(LINKERS)}")
    return names


def sign_v4(method: str, url: str, headers: Dict[str, str], payload_hash: str, access_key: str, secret_key: str, region: str, when: float, service: str = "s3") -> Dict[str, str]:
    parts = urllib.parse.urlsplit(url)
    amz_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(when))
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    signed = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    signed.update({"host": parts.netloc, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date})
    names = sorted(signed)
    query = sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    canonical_query = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}" for k, v in query)
    canonical = "\n".join([
        method,
        parts.path or "/",
        canonical_query,
        "".join(f"{name}:{signed[name]}\n" for name in names),
        ";".join(names),
        payload_hash,
    ])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret_key).encode()
    for part in (amz_date[:8], region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    result = {k: v for k, v in headers.items()}
    result["x-amz-date"] = amz_date
    result["x-amz-content-sha256"] = payload_hash
    result["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={';'.join(names)}, Signature={signature}"
    return result


class LocalRemote:
    def __init__(self, root: Path):
        self.root = root
        self.name = str(root)

    def keys(self) -> Set[str]:
        base = self.root / "files" / "md5"
        if not base.is_dir():
            return set()
        return {md5 for md5 in (key_to_md5(p.relative_to(self.root).as_posix()) for p in base.rglob("*") if p.is_file()) if md5}

    def upload(self, md5: str, source: Path) -> None:
        target = self.root / object_key(md5)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".part")
        shutil.copyfile(source, temp)
        os.replace(temp, target)

    def download(self, md5: str) -> bytes:
        source = self.root / object_key(md5)
        if not source.is_file():
            raise MiniDvcError(f"{md5} is missing from remote {self.name}")
        return source.read_bytes()

    def delete(self, md5: str) -> None:
        (self.root / object_key(md5)).unlink()


class S3Remote:
    def __init__(self, bucket: str, prefix: str, endpoint: str, access_key: str, secret_key: str, region: str = "us-east-1",
                 opener: Optional[Callable[..., Any]] = None, clock: Callable[[], float] = time.time):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.endpoint = endpoint.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.opener = opener or urllib.request.urlopen
        self.clock = clock
        self.name = f"s3://{bucket}/{self.prefix}"

    def object_name(self, md5: str) -> str:
        return f"{self.prefix}/{object_key(md5)}" if self.prefix else object_key(md5)

    def url(self, key: str = "", query: Sequence[Tuple[str, str]] = ()) -> str:
        path = "/" + urllib.parse.quote(self.bucket, safe="") + ("/" + urllib.parse.quote(key, safe="/-_.~") if key else "")
        encoded = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}" for k, v in sorted(query))
        return self.endpoint + path + ("?" + encoded if encoded else "")

    def request(self, method: str, key: str = "", query: Sequence[Tuple[str, str]] = (), body: bytes = b"") -> Tuple[int, bytes]:
        url = self.url(key, query)
        payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        extra = {"Content-Type": "application/octet-stream"} if method == "PUT" else {}
        headers = sign_v4(method, url, extra, payload_hash, self.access_key, self.secret_key, self.region, self.clock())
        req = urllib.request.Request(url, data=body if method == "PUT" else None, method=method, headers=headers)
        try:
            with self.opener(req, timeout=60) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            data = exc.read() or b""
            code = re.search(rb"<Code>([^<]*)</Code>", data)
            reason = code.group(1).decode() if code else exc.reason
            if exc.code == 404 and method in ("GET", "HEAD") and key:
                raise MiniDvcError(f"{key} is missing from remote {self.name} (HTTP 404 {reason})") from exc
            raise MiniDvcError(f"S3 {method} {url}: HTTP {exc.code} {reason}") from exc
        except urllib.error.URLError as exc:
            raise MiniDvcError(f"cannot reach {self.endpoint}: {exc.reason}") from exc

    def keys(self) -> Set[str]:
        found: Set[str] = set()
        token = ""
        prefix = (self.prefix + "/" if self.prefix else "") + "files/md5/"
        while True:
            query = [("list-type", "2"), ("prefix", prefix)] + ([("continuation-token", token)] if token else [])
            _, body = self.request("GET", "", query)
            root = ET.fromstring(body)
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1] == "Key":
                    md5 = key_to_md5(element.text or "")
                    if md5:
                        found.add(md5)
            truncated = next((e.text for e in root if e.tag.rsplit("}", 1)[-1] == "IsTruncated"), "false")
            token = next((e.text or "" for e in root if e.tag.rsplit("}", 1)[-1] == "NextContinuationToken"), "")
            if truncated != "true" or not token:
                return found

    def upload(self, md5: str, source: Path) -> None:
        self.request("PUT", self.object_name(md5), body=source.read_bytes())

    def download(self, md5: str) -> bytes:
        return self.request("GET", self.object_name(md5))[1]

    def delete(self, md5: str) -> None:
        self.request("DELETE", self.object_name(md5))


Remote = Any


class GitReader:
    def __init__(self, root: Path, runner: Callable[..., Any] = subprocess.run):
        self.root = root
        self.runner = runner

    def git(self, *args: str) -> bytes:
        result = self.runner(["git", "-C", str(self.root), *args], capture_output=True)
        if result.returncode != 0:
            raise MiniDvcError(f"git {' '.join(args)}: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout

    def commits(self) -> List[str]:
        return self.git("rev-list", "--all").decode().split()

    def tracked_files(self, commit: str) -> Dict[str, str]:
        files: Dict[str, str] = {}
        for record in self.git("ls-tree", "-r", "-z", commit).split(b"\0"):
            if not record:
                continue
            meta, path = record.decode().split("\t", 1)
            kind, blob = meta.split()[1:3]
            if kind == "blob" and (path.endswith(".dvc") or path.rsplit("/", 1)[-1] == "dvc.lock"):
                files[path] = blob
        return files

    def blob(self, sha: str) -> str:
        return self.git("cat-file", "blob", sha).decode()


def outs_from_text(text: str, path: str) -> List[Out]:
    data = load_yaml(text) or {}
    base = posix_dirname(path)
    outs: List[Out] = []
    if path.rsplit("/", 1)[-1] == "dvc.lock":
        for name, stage in (data.get("stages") or {}).items():
            for entry in stage.get("outs") or []:
                outs.append(Out(join_posix(base, entry["path"]), info_from_entry(entry, path), f"{path}:{name}"))
        return outs
    for entry in data.get("outs") or []:
        outs.append(Out(join_posix(base, entry["path"]), info_from_entry(entry, path), path))
    return outs


def posix_dirname(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def join_posix(base: str, name: str) -> str:
    return os.path.normpath(f"{base}/{name}" if base else name).replace(os.sep, "/")


@dataclass
class Stage:
    name: str
    cmd: str
    deps: List[str]
    outs: List[str]


@dataclass
class Repo:
    root: Path
    linkers: Dict[str, Callable[[Path, Path], None]] = field(default_factory=lambda: dict(LINKERS))

    @property
    def dvc_dir(self) -> Path:
        return self.root / ".dvc"

    @property
    def cache(self) -> Cache:
        return Cache(self.dvc_dir / "cache")

    @classmethod
    def find(cls, start: Path) -> "Repo":
        current = start.resolve()
        for candidate in [current, *current.parents]:
            if (candidate / ".dvc").is_dir():
                return cls(candidate)
        raise UsageError(f"{start}: not inside a DVC repository (no .dvc directory); run `minidvc init` first")

    @classmethod
    def init(cls, root: Path) -> "Repo":
        repo = cls(root.resolve())
        repo.dvc_dir.mkdir(parents=True, exist_ok=True)
        config = repo.dvc_dir / "config"
        if not config.exists():
            config.write_text("")
        (repo.dvc_dir / ".gitignore").write_text(DVC_GITIGNORE)
        ignore = repo.root / ".dvcignore"
        if not ignore.exists():
            ignore.write_text("# Add patterns of files dvc should ignore, which could improve\n# the performance. Learn more at\n# https://dvc.org/doc/user-guide/dvcignore\n")
        return repo

    def rel(self, path: Path) -> str:
        full = Path(os.path.abspath(path))
        return (Path(os.path.realpath(full.parent)) / full.name).relative_to(self.root).as_posix()

    def config(self) -> Dict[str, Dict[str, str]]:
        return read_config(self.dvc_dir / "config")

    def link_order(self, override: Optional[str] = None) -> List[str]:
        configured = override or self.config().get("cache", {}).get("type")
        return parse_links(configured) if configured else list(DEFAULT_LINKS)

    def pointer_files(self) -> List[str]:
        found: List[str] = []
        for current, dirs, files in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            found.extend(self.rel(Path(current) / name) for name in files if name.endswith(".dvc"))
        return sorted(found)

    def workspace_outs(self) -> List[Out]:
        outs: List[Out] = []
        for path in self.pointer_files():
            outs.extend(outs_from_text((self.root / path).read_text(), path))
        if (self.root / "dvc.lock").exists():
            outs.extend(outs_from_text((self.root / "dvc.lock").read_text(), "dvc.lock"))
        return outs

    def add_gitignore(self, path: str) -> None:
        directory = self.root / posix_dirname(path)
        entry = "/" + path.rsplit("/", 1)[-1]
        ignore = directory / ".gitignore"
        existing = ignore.read_text().splitlines() if ignore.exists() else []
        if entry not in existing:
            text = ignore.read_text() if ignore.exists() else ""
            if text and not text.endswith("\n"):
                text += "\n"
            ignore.write_text(text + entry + "\n")

    def commit_path(self, path: Path) -> HashInfo:
        info, entries, data = hash_path(path)
        if info.is_dir:
            for entry in entries:
                self.cache.put_file(path / entry.relpath, entry.md5)
            self.cache.put_bytes(data, info.md5)
        else:
            self.cache.put_file(path, info.md5)
        return info

    def add(self, targets: Sequence[str]) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for target in targets:
            path = Path(os.path.abspath(target))
            rel = self.rel(path)
            if not path.exists():
                raise MiniDvcError(f"{rel}: no such file or directory")
            info = self.commit_path(path)
            pointer = path.with_name(path.name + ".dvc")
            pointer.write_text(pointer_text(info, path.name))
            self.add_gitignore(rel)
            results.append({"path": rel, "pointer": self.rel(pointer), **info.to_dict()})
        return results

    def missing_objects(self, info: HashInfo) -> List[str]:
        if not self.cache.has(info.md5):
            return [info.md5]
        if info.is_dir:
            manifest = self.cache.manifest(info.md5) or {}
            return sorted({md5 for md5 in manifest.values() if not self.cache.has(md5)})
        return []

    def out_state(self, out: Out) -> Optional[str]:
        if self.missing_objects(out.info):
            return "not in cache"
        path = self.root / out.path
        if not path.exists():
            return "deleted"
        current, _, _ = hash_path(path)
        return None if current.md5 == out.info.md5 else "modified"

    def status(self) -> Dict[str, List[Any]]:
        report: Dict[str, List[Any]] = {}
        for pointer in self.pointer_files():
            changed = {}
            for out in outs_from_text((self.root / pointer).read_text(), pointer):
                state = self.out_state(out)
                if state:
                    changed[out.path] = state
            if changed:
                report[pointer] = [{"changed outs": changed}]
        for stage, reasons in self.stage_status().items():
            if reasons:
                report[stage] = reasons
        return report

    def unsaved(self, path: Path) -> bool:
        if path.is_symlink() or not path.exists():
            return False
        if path.is_dir():
            return any(not self.cache.has(file_md5(full)) for _, full in walk_files(path))
        return not self.cache.has(file_md5(path))

    def checkout_out(self, out: Out, strategies: Sequence[str], force: bool) -> Dict[str, Any]:
        path = self.root / out.path
        missing = self.missing_objects(out.info)
        if missing:
            raise MiniDvcError(f"{out.path}: {len(missing)} object(s) missing from cache (first {missing[0]}); run `minidvc pull`")
        if not out.info.is_dir:
            if path.is_file() and not path.is_symlink() and file_md5(path) == out.info.md5:
                return {"path": out.path, "action": "unchanged", "links": []}
            if not force and self.unsaved(path):
                raise MiniDvcError(f"{out.path}: workspace version is not in the cache and would be lost; `minidvc add` it or use --force")
            if path.is_dir():
                shutil.rmtree(path)
            return {"path": out.path, "action": "restored", "links": [link_file(self.cache.path(out.info.md5), path, strategies, self.linkers)]}
        wanted = self.cache.manifest(out.info.md5) or {}
        if path.exists() and not path.is_dir():
            if not force and self.unsaved(path):
                raise MiniDvcError(f"{out.path}: workspace file is not in the cache and would be lost; use --force")
            remove_path(path)
        present = {rel: full for rel, full in walk_files(path)} if path.is_dir() else {}
        current = {rel: file_md5(full) for rel, full in present.items()}
        stale = [rel for rel in present if current[rel] != wanted.get(rel)]
        if not force:
            lost = [rel for rel in stale if not self.cache.has(current[rel])]
            if lost:
                raise MiniDvcError(f"{out.path}: {len(lost)} file(s) not in the cache would be lost (first {lost[0]}); use --force")
        for rel in stale:
            remove_path(present[rel])
        links: Set[str] = set()
        restored = 0
        for rel, md5 in sorted(wanted.items()):
            if current.get(rel) == md5:
                continue
            links.add(link_file(self.cache.path(md5), path / rel, strategies, self.linkers))
            restored += 1
        path.mkdir(parents=True, exist_ok=True)
        for current_dir, _, _ in os.walk(path, topdown=False):
            if Path(current_dir) != path and not os.listdir(current_dir):
                os.rmdir(current_dir)
        action = "restored" if restored or stale else "unchanged"
        return {"path": out.path, "action": action, "links": sorted(links), "files_restored": restored, "files_removed": len([r for r in stale if r not in wanted])}

    def checkout(self, links: Optional[str] = None, force: bool = False) -> List[Dict[str, Any]]:
        strategies = self.link_order(links)
        return [self.checkout_out(out, strategies, force) for out in self.workspace_outs()]

    def remote(self, name: Optional[str] = None, env: Optional[Dict[str, str]] = None, opener: Optional[Callable[..., Any]] = None) -> Remote:
        env = os.environ if env is None else env
        config = self.config()
        name = name or config.get("core", {}).get("remote")
        if not name:
            raise UsageError("no remote given and no default remote; run `minidvc remote add -d NAME URL`")
        section = config.get(f'remote "{name}"')
        if section is None or "url" not in section:
            raise UsageError(f"remote {name!r} is not configured in .dvc/config")
        url = section["url"]
        if url.startswith("s3://"):
            bucket, _, prefix = url[5:].partition("/")
            access = section.get("access_key_id") or env.get("AWS_ACCESS_KEY_ID")
            secret = section.get("secret_access_key") or env.get("AWS_SECRET_ACCESS_KEY")
            if not access or not secret:
                raise UsageError("S3 remote needs AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
            region = section.get("region") or env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1"
            endpoint = section.get("endpointurl") or f"https://s3.{region}.amazonaws.com"
            return S3Remote(bucket, prefix, endpoint, access, secret, region, opener=opener)
        if "://" in url:
            raise UsageError(f"remote URL {url!r}: only s3:// and local paths are supported")
        path = Path(url)
        return LocalRemote(path if path.is_absolute() else (self.dvc_dir / path).resolve())

    def add_remote(self, name: str, url: str, endpoint: Optional[str], default: bool) -> Dict[str, Any]:
        config = self.config()
        section = config.setdefault(f'remote "{name}"', {})
        section["url"] = url
        if endpoint:
            section["endpointurl"] = endpoint
        if default:
            config.setdefault("core", {})["remote"] = name
        ordered = {"core": config.pop("core")} if "core" in config else {}
        ordered.update(config)
        write_config(self.dvc_dir / "config", ordered)
        return {"remote": name, "url": url, "default": default}

    def referenced(self, outs: Iterable[Out], remote: Optional[Remote] = None) -> Set[str]:
        keep: Set[str] = set()
        for out in outs:
            keep.add(out.info.md5)
            if out.info.is_dir:
                manifest = self.cache.manifest(out.info.md5)
                if manifest is None and remote is not None:
                    data = remote.download(out.info.md5)
                    check_md5(data, out.info.md5)
                    self.cache.put_bytes(data, out.info.md5)
                    manifest = parse_manifest(data)
                if manifest is None:
                    raise MiniDvcError(f"{out.path}: directory manifest {out.info.md5} is in neither the cache nor a remote; cannot list its files")
                keep.update(manifest.values())
        return keep

    def push(self, remote: Remote) -> Dict[str, Any]:
        wanted = self.referenced(self.workspace_outs())
        on_remote = remote.keys()
        missing = sorted(md5 for md5 in wanted if not self.cache.has(md5))
        todo = sorted((md5 for md5 in wanted - on_remote if self.cache.has(md5)), key=lambda md5: (md5.endswith(DIR_SUFFIX), md5))
        for md5 in todo:
            remote.upload(md5, self.cache.path(md5))
        return {"remote": remote.name, "uploaded": todo, "already_on_remote": len(wanted & on_remote), "missing_from_cache": missing}

    def fetch(self, remote: Remote) -> Dict[str, Any]:
        outs = self.workspace_outs()
        downloaded: List[str] = []
        for out in outs:
            if out.info.is_dir and not self.cache.has(out.info.md5):
                data = remote.download(out.info.md5)
                check_md5(data, out.info.md5)
                self.cache.put_bytes(data, out.info.md5)
                downloaded.append(out.info.md5)
        for md5 in sorted(self.referenced(outs)):
            if not self.cache.has(md5):
                data = remote.download(md5)
                check_md5(data, md5)
                self.cache.put_bytes(data, md5)
                downloaded.append(md5)
        return {"remote": remote.name, "downloaded": downloaded}

    def gc(self, all_commits: bool, remote: Optional[Remote] = None, dry_run: bool = False, git: Optional[GitReader] = None) -> Dict[str, Any]:
        outs = self.workspace_outs()
        commits: List[str] = []
        if all_commits:
            git = git or GitReader(self.root)
            commits = git.commits()
            blobs: Dict[str, str] = {}
            for commit in commits:
                for path, blob in git.tracked_files(commit).items():
                    blobs.setdefault(blob, path)
            for blob, path in sorted(blobs.items()):
                outs.extend(outs_from_text(git.blob(blob), path))
        keep = self.referenced(outs, remote)
        cached = self.cache.objects()
        drop_cache = sorted(cached - keep)
        drop_remote: List[str] = []
        if remote is not None:
            drop_remote = sorted(remote.keys() - keep)
        if not dry_run:
            for md5 in drop_cache:
                self.cache.remove(md5)
            for md5 in drop_remote:
                remote.delete(md5)
        return {"commits": len(commits), "referenced": len(keep), "removed_from_cache": drop_cache, "removed_from_remote": drop_remote, "dry_run": dry_run}

    def stages(self) -> List[Stage]:
        path = self.root / "dvc.yaml"
        if not path.exists():
            return []
        data = load_yaml(path.read_text()) or {}
        stages: List[Stage] = []
        for name, body in (data.get("stages") or {}).items():
            if not isinstance(body, dict) or not isinstance(body.get("cmd"), str):
                raise UsageError(f"dvc.yaml: stage {name!r} needs a cmd string")
            stages.append(Stage(name, body["cmd"], [str(d) for d in body.get("deps") or []], [str(o) for o in body.get("outs") or []]))
        return order_stages(stages)

    def lock(self) -> Dict[str, Any]:
        path = self.root / "dvc.lock"
        data = load_yaml(path.read_text()) if path.exists() else None
        return (data or {}).get("stages") or {}

    def entry(self, path: str) -> Optional[Dict[str, Any]]:
        full = self.root / path
        if not full.exists():
            return None
        info, _, _ = hash_path(full)
        data: Dict[str, Any] = {"path": path, "hash": "md5", "md5": info.md5, "size": info.size}
        if info.is_dir:
            data["nfiles"] = info.nfiles
        return data

    def stage_reasons(self, stage: Stage, locked: Optional[Dict[str, Any]]) -> List[Any]:
        if locked is None:
            return ["not in dvc.lock"]
        reasons: List[Any] = []
        if locked.get("cmd") != stage.cmd:
            reasons.append("changed command")
        before = {e["path"]: e.get("md5") for e in locked.get("deps") or []}
        deps: Dict[str, str] = {}
        for path in stage.deps:
            current = self.entry(path)
            if current is None:
                deps[path] = "deleted"
            elif path not in before:
                deps[path] = "new"
            elif current["md5"] != before[path]:
                deps[path] = "modified"
        deps.update({path: "deleted" for path in before if path not in stage.deps})
        if deps:
            reasons.append({"changed deps": deps})
        locked_outs = {e["path"]: e.get("md5") for e in locked.get("outs") or []}
        outs: Dict[str, str] = {}
        for path in stage.outs:
            current = self.entry(path)
            if current is None:
                outs[path] = "deleted"
            elif current["md5"] != locked_outs.get(path):
                outs[path] = "modified"
        if outs:
            reasons.append({"changed outs": outs})
        return reasons

    def stage_status(self) -> Dict[str, List[Any]]:
        locked = self.lock()
        return {stage.name: self.stage_reasons(stage, locked.get(stage.name)) for stage in self.stages()}

    def repro(self, runner: Callable[..., Any] = subprocess.run) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        lock = self.lock()
        stages = self.stages()
        for stage in stages:
            reasons = self.stage_reasons(stage, lock.get(stage.name))
            if not reasons:
                results.append({"stage": stage.name, "ran": False, "reasons": []})
                continue
            missing = [dep for dep in stage.deps if not (self.root / dep).exists()]
            if missing:
                raise MiniDvcError(f"stage {stage.name!r}: dependency {missing[0]!r} does not exist")
            for out in stage.outs:
                remove_path(self.root / out)
            completed = runner(stage.cmd, shell=True, cwd=str(self.root))
            if completed.returncode != 0:
                raise MiniDvcError(f"stage {stage.name!r}: command exited with {completed.returncode}: {stage.cmd}")
            outs = []
            for out in stage.outs:
                if not (self.root / out).exists():
                    raise MiniDvcError(f"stage {stage.name!r}: output {out!r} was not created")
                self.commit_path(self.root / out)
                self.add_gitignore(out)
                outs.append(self.entry(out))
            lock[stage.name] = {
                "cmd": stage.cmd,
                "deps": sorted((self.entry(dep) for dep in stage.deps), key=lambda e: e["path"]),
                "outs": sorted(outs, key=lambda e: e["path"]),
            }
            current = {s.name: lock[s.name] for s in stages if s.name in lock}
            (self.root / "dvc.lock").write_text(to_yaml({"schema": "2.0", "stages": current}))
            results.append({"stage": stage.name, "ran": True, "reasons": reasons})
        return results


def check_md5(data: bytes, md5: str) -> None:
    actual = hashlib.md5(data).hexdigest()
    if actual != md5.replace(DIR_SUFFIX, ""):
        raise MiniDvcError(f"object {md5} is corrupt: content hashes to {actual}")


def produces(stage: Stage, path: str) -> bool:
    return any(path == out or path.startswith(out.rstrip("/") + "/") for out in stage.outs)


def order_stages(stages: List[Stage]) -> List[Stage]:
    upstream = {s.name: {o.name for o in stages if o is not s and any(produces(o, dep) for dep in s.deps)} for s in stages}
    ordered: List[Stage] = []
    done: Set[str] = set()
    while len(ordered) < len(stages):
        ready = [s for s in stages if s.name not in done and upstream[s.name] <= done]
        if not ready:
            raise UsageError("dvc.yaml: the stages form a cycle: " + ", ".join(sorted(s.name for s in stages if s.name not in done)))
        ordered.append(ready[0])
        done.add(ready[0].name)
    return ordered


def print_status(report: Dict[str, List[Any]]) -> None:
    if not report:
        print("Data and pipelines are up to date.")
        return
    for name, reasons in report.items():
        print(f"{name}:")
        for reason in reasons:
            if isinstance(reason, str):
                print(f"\t{reason}")
                continue
            for title, paths in reason.items():
                print(f"\t{title}:")
                for path, state in paths.items():
                    print(f"\t\t{state + ':':<20}{path}")


def human(command: str, result: Any) -> None:
    if command == "add":
        for item in result:
            extra = f"{item['nfiles']} files, " if "nfiles" in item else ""
            print(f"added {item['path']}  md5 {item['md5']}  {extra}{item['size']} bytes  -> {item['pointer']}")
    elif command == "hash":
        extra = f"  nfiles {result['nfiles']}" if "nfiles" in result else ""
        print(f"{result['md5']}  size {result['size']}{extra}  {result['path']}")
    elif command == "status":
        print_status(result)
    elif command in ("checkout", "pull"):
        rows = result["checkout"] if command == "pull" else result
        if command == "pull":
            print(f"downloaded {len(result['fetch']['downloaded'])} object(s) from {result['fetch']['remote']}")
        for row in rows:
            if row["action"] != "unchanged":
                print(f"restored {row['path']} ({', '.join(row['links'])})")
        print(f"{sum(r['action'] != 'unchanged' for r in rows)} restored, {sum(r['action'] == 'unchanged' for r in rows)} unchanged")
    elif command == "push":
        print(f"pushed {len(result['uploaded'])} object(s) to {result['remote']}, {result['already_on_remote']} already there")
        for md5 in result["missing_from_cache"]:
            print(f"WARNING: {md5} is referenced but missing from the cache")
    elif command == "fetch":
        print(f"downloaded {len(result['downloaded'])} object(s) from {result['remote']}")
    elif command == "gc":
        verb = "would remove" if result["dry_run"] else "removed"
        print(f"{result['commits']} commit(s) scanned, {result['referenced']} object(s) referenced")
        print(f"{verb} {len(result['removed_from_cache'])} object(s) from the cache and {len(result['removed_from_remote'])} from the remote")
    elif command == "repro":
        for row in result:
            if row["ran"]:
                print(f"Running stage '{row['stage']}': {json.dumps(row['reasons'])}")
            else:
                print(f"Stage '{row['stage']}' didn't change, skipping")
    elif command in ("init", "remote"):
        print(json.dumps(result))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minidvc", description="Version data outside Git the way DVC does: content-addressed cache, pointer files, remotes, gc and a pipeline lock.")
    parser.add_argument("-C", "--repo", default=".", help="directory inside the repository (default: .)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create .dvc/ with config, cache and .gitignore")
    add = sub.add_parser("add", help="hash files or directories into the cache and write .dvc pointer files")
    add.add_argument("targets", nargs="+")
    hashp = sub.add_parser("hash", help="print the md5 DVC would record for a file or directory")
    hashp.add_argument("path")
    status = sub.add_parser("status", help="compare the workspace and the cache with pointers and dvc.lock")
    status.add_argument("-q", "--quiet", action="store_true", help="no output; exit 1 when anything changed")
    checkout = sub.add_parser("checkout", help="make the workspace match the pointer files")
    checkout.add_argument("--link", help="comma-separated link order, e.g. reflink,hardlink,copy (default: cache.type or reflink,copy)")
    checkout.add_argument("-f", "--force", action="store_true", help="overwrite workspace data that is not in the cache")
    remote = sub.add_parser("remote", help="configure remotes in .dvc/config")
    remote_sub = remote.add_subparsers(dest="remote_command", required=True)
    remote_add = remote_sub.add_parser("add")
    remote_add.add_argument("name")
    remote_add.add_argument("url", help="s3://bucket/prefix or a directory")
    remote_add.add_argument("--endpoint", help="S3-compatible endpoint URL (endpointurl)")
    remote_add.add_argument("-d", "--default", action="store_true")
    for name in ("push", "fetch", "pull"):
        cmd = sub.add_parser(name, help=f"{name} objects referenced by the workspace")
        cmd.add_argument("-r", "--remote")
        if name == "pull":
            cmd.add_argument("--link")
            cmd.add_argument("-f", "--force", action="store_true")
    gc = sub.add_parser("gc", help="delete cache (and remote) objects no pointer references")
    gc.add_argument("--all-commits", action="store_true", help="keep objects referenced by any commit reachable from a Git ref")
    gc.add_argument("--cloud", action="store_true", help="also delete unreferenced objects from the remote")
    gc.add_argument("-r", "--remote")
    gc.add_argument("--dry-run", action="store_true")
    sub.add_parser("repro", help="run the stages of dvc.yaml whose command, deps or outs changed")
    return parser


def run(args: argparse.Namespace) -> Tuple[Any, int]:
    start = Path(args.repo)
    if args.command == "init":
        repo = Repo.init(start)
        return {"initialized": str(repo.root)}, 0
    if args.command == "hash":
        info, _, _ = hash_path(start / args.path)
        return {"path": args.path, **info.to_dict()}, 0
    repo = Repo.find(start)
    if args.command == "add":
        return repo.add([str(start / t) for t in args.targets]), 0
    if args.command == "status":
        report = repo.status()
        return report, (1 if args.quiet and report else 0)
    if args.command == "checkout":
        return repo.checkout(args.link, args.force), 0
    if args.command == "remote":
        return repo.add_remote(args.name, args.url, args.endpoint, args.default), 0
    if args.command == "push":
        result = repo.push(repo.remote(args.remote))
        return result, (1 if result["missing_from_cache"] else 0)
    if args.command == "fetch":
        return repo.fetch(repo.remote(args.remote)), 0
    if args.command == "pull":
        fetched = repo.fetch(repo.remote(args.remote))
        return {"fetch": fetched, "checkout": repo.checkout(args.link, args.force)}, 0
    if args.command == "gc":
        remote = repo.remote(args.remote) if args.cloud else None
        return repo.gc(args.all_commits, remote, args.dry_run), 0
    if args.command == "repro":
        return repo.repro(), 0
    raise UsageError(f"unknown command {args.command}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        result, code = run(args)
    except MiniDvcError as exc:
        print(f"minidvc: {exc}", file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"minidvc: {exc}", file=sys.stderr)
        return 1
    if args.command == "status" and getattr(args, "quiet", False):
        return code
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        human(args.command, result)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
