"""minisbom: inventory a root filesystem or an exported image (dpkg, apk, Python and npm), write CycloneDX 1.6 or SPDX 2.3 JSON, diff two SBOMs, and match packages against OSV advisories using each ecosystem's own version order."""
from __future__ import annotations

import argparse
import base64
import csv
import functools
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from email.parser import HeaderParser
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, unquote

TOOL = "minisbom"
TOOL_VERSION = "0.1.0"
TYPES = ("deb", "apk", "pypi", "npm")
ECOSYSTEM_LABELS = {"deb": "dpkg", "apk": "apk", "pypi": "python", "npm": "npm"}
DPKG_STATUS = "var/lib/dpkg/status"
DPKG_STATUS_DIR = "var/lib/dpkg/status.d/"
DPKG_GONE_STATES = ("not-installed", "config-files")
APK_DATABASES = ("lib/apk/db/installed", "usr/lib/apk/db/installed")
OS_RELEASE_FILES = ("etc/os-release", "usr/lib/os-release")
PYTHON_METADATA = re.compile(r"(?:^|/)[^/]+\.(?:dist-info/METADATA|egg-info/PKG-INFO)$")
NPM_LOCKS = ("package-lock.json", "npm-shrinkwrap.json")
SKIPPED_TOP_DIRS = ("proc", "sys", "dev")
MAX_SYMLINKS = 40
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SOURCE_FIELD = re.compile(r"^(\S+)(?:\s+\((\S+)\))?$")
DEBIAN_CODENAMES = {"buster": "10", "bullseye": "11", "bookworm": "12", "trixie": "13", "forky": "14"}
OSV_FAMILIES = {"Debian": "deb", "Ubuntu": "deb", "Alpine": "apk", "PyPI": "pypi", "npm": "npm"}
SPDX_IDS = (
    "0BSD", "Apache-2.0", "Artistic-2.0", "BSD-2-Clause", "BSD-3-Clause", "BSL-1.0", "CC0-1.0", "curl",
    "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only", "GPL-3.0-or-later", "ISC", "LGPL-2.0-or-later",
    "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0-only", "LGPL-3.0-or-later", "MIT", "MIT-0", "MPL-2.0",
    "OpenSSL", "PSF-2.0", "Python-2.0", "Unlicense", "X11", "Zlib",
)
LICENSE_ALIASES = {
    "expat": "MIT",
    "mit license": "MIT",
    "gpl-2": "GPL-2.0-only",
    "gpl-2+": "GPL-2.0-or-later",
    "gpl-3": "GPL-3.0-only",
    "gpl-3+": "GPL-3.0-or-later",
    "lgpl-2+": "LGPL-2.0-or-later",
    "lgpl-2.1": "LGPL-2.1-only",
    "lgpl-2.1+": "LGPL-2.1-or-later",
    "lgpl-3": "LGPL-3.0-only",
    "lgpl-3+": "LGPL-3.0-or-later",
    "apache 2.0": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0",
    "isc license (iscl)": "ISC",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "python software foundation license": "PSF-2.0",
    "the unlicense (unlicense)": "Unlicense",
}
SPDX_BY_LOWER = {name.lower(): name for name in SPDX_IDS}


class InputError(ValueError):
    pass


class VersionError(InputError):
    pass


def sign(number: int) -> int:
    return (number > 0) - (number < 0)


def is_digit(char: str) -> bool:
    return "0" <= char <= "9"


def is_alpha(char: str) -> bool:
    return "a" <= char <= "z" or "A" <= char <= "Z"


def dpkg_order(char: str) -> int:
    if not char or is_digit(char):
        return 0
    if is_alpha(char):
        return ord(char)
    if char == "~":
        return -1
    return ord(char) + 256


def dpkg_verrevcmp(a: str, b: str) -> int:
    i = j = 0
    while i < len(a) or j < len(b):
        first_diff = 0
        while (i < len(a) and not is_digit(a[i])) or (j < len(b) and not is_digit(b[j])):
            ac = dpkg_order(a[i] if i < len(a) else "")
            bc = dpkg_order(b[j] if j < len(b) else "")
            if ac != bc:
                return sign(ac - bc)
            i += 1
            j += 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        while i < len(a) and j < len(b) and is_digit(a[i]) and is_digit(b[j]):
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and is_digit(a[i]):
            return 1
        if j < len(b) and is_digit(b[j]):
            return -1
        if first_diff:
            return sign(first_diff)
    return 0


def parse_dpkg(version: str) -> Tuple[int, str, str]:
    text = version.strip()
    epoch, colon, rest = text.partition(":")
    if not colon:
        epoch, rest = "0", text
    elif not epoch.isdigit():
        raise VersionError(f"dpkg version {version!r}: the epoch {epoch!r} is not a number")
    upstream, dash, revision = rest.rpartition("-")
    if not dash:
        upstream, revision = rest, ""
    if not upstream or re.search(r"[^A-Za-z0-9.+~:-]", upstream) or re.search(r"[^A-Za-z0-9.+~]", revision):
        raise VersionError(f"dpkg version {version!r}: allowed characters are letters, digits and . + ~ (and - : in the upstream part)")
    return int(epoch), upstream, revision


def compare_dpkg(a: str, b: str) -> int:
    epoch_a, upstream_a, revision_a = parse_dpkg(a)
    epoch_b, upstream_b, revision_b = parse_dpkg(b)
    if epoch_a != epoch_b:
        return sign(epoch_a - epoch_b)
    return dpkg_verrevcmp(upstream_a, upstream_b) or dpkg_verrevcmp(revision_a, revision_b)


APK_INVALID, APK_DIGIT_OR_ZERO, APK_DIGIT, APK_LETTER, APK_SUFFIX, APK_SUFFIX_NO, APK_REVISION_NO, APK_END = -1, 0, 1, 2, 3, 4, 5, 6
APK_PRE_SUFFIXES = ("alpha", "beta", "pre", "rc")
APK_POST_SUFFIXES = ("cvs", "svn", "git", "hg", "p")


def apk_next_kind(kind: int, text: str, pos: int) -> Tuple[int, int]:
    if pos >= len(text):
        return APK_END, pos
    char = text[pos]
    if kind in (APK_DIGIT, APK_DIGIT_OR_ZERO) and "a" <= char <= "z":
        new = APK_LETTER
    elif kind == APK_LETTER and is_digit(char):
        new = APK_DIGIT
    elif kind == APK_SUFFIX and is_digit(char):
        new = APK_SUFFIX_NO
    else:
        new = APK_INVALID
        if char == ".":
            new = APK_DIGIT_OR_ZERO
        elif char == "_":
            new = APK_SUFFIX
        elif char == "-" and text[pos + 1:pos + 2] == "r":
            new = APK_REVISION_NO
            pos += 1
        pos += 1
    allowed_backwards = (
        (new == APK_DIGIT_OR_ZERO and kind == APK_DIGIT)
        or (new == APK_SUFFIX and kind == APK_SUFFIX_NO)
        or (new == APK_DIGIT and kind == APK_LETTER)
    )
    if new < kind and not allowed_backwards:
        new = APK_INVALID
    return new, pos


def apk_token(kind: int, text: str, pos: int) -> Tuple[int, int, int]:
    if pos >= len(text):
        return 0, APK_END, pos
    end, value, after = pos, 0, APK_INVALID
    if kind == APK_DIGIT_OR_ZERO and text[pos] == "0":
        while end < len(text) and text[end] == "0":
            end += 1
        value, after = pos - end, APK_DIGIT
    elif kind in (APK_DIGIT_OR_ZERO, APK_DIGIT, APK_SUFFIX_NO, APK_REVISION_NO):
        while end < len(text) and is_digit(text[end]):
            value = value * 10 + int(text[end])
            end += 1
    elif kind == APK_LETTER:
        value, end = ord(text[pos]), pos + 1
    elif kind == APK_SUFFIX:
        for index, suffix in enumerate(APK_PRE_SUFFIXES):
            if text.startswith(suffix, pos):
                value, after, end = index - len(APK_PRE_SUFFIXES), APK_SUFFIX_NO, pos + len(suffix)
                break
        else:
            for index, suffix in enumerate(APK_POST_SUFFIXES):
                if text.startswith(suffix, pos):
                    value, after, end = index, APK_SUFFIX_NO, pos + len(suffix)
                    break
            else:
                return -1, APK_INVALID, pos
    else:
        return -1, APK_INVALID, pos
    if end >= len(text):
        return value, APK_END, end
    if after != APK_INVALID:
        return value, after, end
    kind, end = apk_next_kind(kind, text, end)
    return value, kind, end


def compare_apk(a: str, b: str) -> int:
    kind_a = kind_b = APK_DIGIT
    pos_a = pos_b = value_a = value_b = 0
    while kind_a == kind_b and kind_a not in (APK_END, APK_INVALID) and value_a == value_b:
        value_a, kind_a, pos_a = apk_token(kind_a, a, pos_a)
        value_b, kind_b, pos_b = apk_token(kind_b, b, pos_b)
    if APK_INVALID in (kind_a, kind_b):
        raise VersionError(f"apk version {a if kind_a == APK_INVALID else b!r} is not valid (expected 1.2.3[a][_rc1][-r0])")
    if value_a != value_b:
        return sign(value_a - value_b)
    if kind_a == kind_b:
        return 0
    if kind_a == APK_SUFFIX and apk_token(kind_a, a, pos_a)[0] < 0:
        return -1
    if kind_b == APK_SUFFIX and apk_token(kind_b, b, pos_b)[0] < 0:
        return 1
    return -1 if kind_a > kind_b else 1


PEP440 = re.compile(
    r"^\s*v?(?:(?P<epoch>[0-9]+)!)?(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?P<pre>[-_.]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_n>[0-9]+)?)?"
    r"(?P<post>(?:-(?P<post_n1>[0-9]+))|(?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?))?"
    r"(?P<dev>[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]+)?)?"
    r"(?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?\s*$",
    re.IGNORECASE,
)
PEP440_PRE = {"a": "a", "alpha": "a", "b": "b", "beta": "b", "c": "rc", "pre": "rc", "preview": "rc", "rc": "rc"}


def pep440_key(version: str) -> Tuple[Any, ...]:
    match = PEP440.match(version)
    if not match:
        raise VersionError(f"{version!r} is not a PEP 440 version (examples: 1.0, 1!2.0, 1.0rc1, 1.0.post2, 1.0.dev3, 1.0+local)")
    release = [int(part) for part in match.group("release").split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    has_pre, has_post, has_dev = bool(match.group("pre_l")), match.group("post") is not None, bool(match.group("dev_l"))
    if has_pre:
        pre = (0, PEP440_PRE[match.group("pre_l").lower()], int(match.group("pre_n") or 0))
    elif not has_post and has_dev:
        pre = (-1, "", 0)
    else:
        pre = (1, "", 0)
    post = (0, int(match.group("post_n1") or match.group("post_n2") or 0)) if has_post else (-1, 0)
    dev = (0, int(match.group("dev_n") or 0)) if has_dev else (1, 0)
    local: Tuple[Any, ...] = (0,)
    if match.group("local"):
        local = (1,) + tuple((1, int(part), "") if part.isdigit() else (0, 0, part.lower()) for part in re.split(r"[-_.]", match.group("local")))
    return int(match.group("epoch") or 0), tuple(release), pre, post, dev, local


def compare_pep440(a: str, b: str) -> int:
    key_a, key_b = pep440_key(a), pep440_key(b)
    return (key_a > key_b) - (key_a < key_b)


SEMVER = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def semver_key(version: str) -> Tuple[Any, ...]:
    match = SEMVER.match(version.strip())
    if not match:
        raise VersionError(f"{version!r} is not a SemVer 2.0.0 version (MAJOR.MINOR.PATCH[-PRERELEASE][+BUILD])")
    core = tuple(int(part) for part in match.group(1, 2, 3))
    if match.group(4) is None:
        return core, (1,)
    identifiers = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in match.group(4).split("."))
    return core, (0, identifiers)


def compare_semver(a: str, b: str) -> int:
    key_a, key_b = semver_key(a), semver_key(b)
    return (key_a > key_b) - (key_a < key_b)


COMPARATORS: Dict[str, Callable[[str, str], int]] = {
    "deb": compare_dpkg,
    "apk": compare_apk,
    "pypi": compare_pep440,
    "npm": compare_semver,
}


def compare_versions(kind: str, a: str, b: str) -> int:
    if kind not in COMPARATORS:
        raise VersionError(f"no version order for type {kind!r}; known: {', '.join(COMPARATORS)}")
    return COMPARATORS[kind](a, b)


def make_purl(kind: str, namespace: str, name: str, version: str, qualifiers: Mapping[str, str]) -> str:
    text = f"pkg:{kind}/"
    if namespace:
        text += "/".join(quote(part, safe=":") for part in namespace.split("/")) + "/"
    text += quote(name, safe=":")
    if version:
        text += "@" + quote(version, safe=":")
    pairs = [f"{key}={quote(value, safe=':/')}" for key, value in sorted(qualifiers.items()) if value]
    return text + ("?" + "&".join(pairs) if pairs else "")


@dataclass
class Purl:
    type: str
    namespace: str
    name: str
    version: str
    qualifiers: Dict[str, str]


def parse_purl(text: str) -> Purl:
    if not text.startswith("pkg:"):
        raise InputError(f"not a package URL: {text!r} (expected pkg:TYPE/NAMESPACE/NAME@VERSION)")
    rest = text[4:].lstrip("/").split("#", 1)[0]
    rest, _, query = rest.partition("?")
    path, at, version = rest.rpartition("@")
    if not at or "/" in version:
        path, version = rest, ""
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        raise InputError(f"package URL {text!r} has no name")
    qualifiers: Dict[str, str] = {}
    for pair in query.split("&"):
        if pair:
            key, _, value = pair.partition("=")
            qualifiers[key.lower()] = unquote(value)
    namespace = "/".join(unquote(segment) for segment in segments[1:-1])
    return Purl(segments[0].lower(), namespace, unquote(segments[-1]), unquote(version), qualifiers)


def name_key(kind: str, name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower() if kind == "pypi" else name


def purl_name(kind: str, name: str) -> str:
    return name.lower().replace("_", "-") if kind == "pypi" else name


def clean(name: str) -> str:
    return "/".join(part for part in name.replace("\\", "/").split("/") if part not in ("", "."))


class Tree:
    label = ""

    def kind(self, path: str) -> Tuple[str, str]:
        raise NotImplementedError

    def paths(self) -> List[str]:
        raise NotImplementedError

    def open_bytes(self, path: str) -> Optional[bytes]:
        raise NotImplementedError

    def close(self) -> None:
        return None

    def resolve(self, path: str) -> str:
        pending = [part for part in path.split("/") if part not in ("", ".")]
        done: List[str] = []
        hops = 0
        while pending:
            part = pending.pop(0)
            if part == "..":
                if done:
                    done.pop()
                continue
            kind, target = self.kind("/".join(done + [part]))
            if kind != "link":
                done.append(part)
                continue
            hops += 1
            if hops > MAX_SYMLINKS:
                raise InputError(f"{self.label}: more than {MAX_SYMLINKS} symlinks while resolving {path}")
            if target.startswith("/"):
                done = []
            pending = [p for p in target.split("/") if p not in ("", ".")] + pending
        return "/".join(done)

    def read(self, path: str) -> Optional[bytes]:
        real = self.resolve(path)
        if self.kind(real)[0] != "file":
            return None
        return self.open_bytes(real)

    def text(self, path: str) -> Optional[str]:
        data = self.read(path)
        return None if data is None else data.decode("utf-8", "replace")


class DirTree(Tree):
    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.label = root
        self._paths: Optional[List[str]] = None

    def kind(self, path: str) -> Tuple[str, str]:
        full = os.path.join(self.root, *path.split("/")) if path else self.root
        if os.path.islink(full):
            return "link", os.readlink(full)
        if os.path.isdir(full):
            return "dir", ""
        if os.path.isfile(full):
            return "file", ""
        return "", ""

    def paths(self) -> List[str]:
        if self._paths is None:
            found: List[str] = []
            for current, dirs, files in os.walk(self.root):
                relative = os.path.relpath(current, self.root).replace(os.sep, "/")
                relative = "" if relative == "." else relative
                if not relative:
                    dirs[:] = [d for d in dirs if d not in SKIPPED_TOP_DIRS]
                dirs.sort()
                found.extend(f"{relative}/{name}" if relative else name for name in sorted(files))
            self._paths = found
        return self._paths

    def open_bytes(self, path: str) -> Optional[bytes]:
        try:
            with open(os.path.join(self.root, *path.split("/")), "rb") as handle:
                return handle.read()
        except OSError:
            return None


class TarTree(Tree):
    def __init__(self, archive: tarfile.TarFile, label: str, spool: Optional[Any] = None) -> None:
        self.archive = archive
        self.label = label
        self.spool = spool
        self.members: Dict[str, tarfile.TarInfo] = {}
        self.dirs = {""}
        for member in archive.getmembers():
            name = clean(member.name)
            if not name:
                continue
            self.members[name] = member
            parts = name.split("/")
            self.dirs.update("/".join(parts[:index]) for index in range(1, len(parts)))

    def kind(self, path: str) -> Tuple[str, str]:
        member = self.members.get(path)
        if member is None:
            return ("dir", "") if path in self.dirs else ("", "")
        if member.issym():
            return "link", member.linkname
        if member.isdir():
            return "dir", ""
        if member.isfile() or member.islnk():
            return "file", ""
        return "", ""

    def paths(self) -> List[str]:
        return sorted(name for name, member in self.members.items() if member.isfile() or member.islnk() or member.issym())

    def open_bytes(self, path: str) -> Optional[bytes]:
        member = self.members[path]
        if member.islnk():
            target = self.members.get(clean(member.linkname))
            if target is None or not target.isfile():
                return None
            member = target
        handle = self.archive.extractfile(member)
        return handle.read() if handle else None

    def close(self) -> None:
        self.archive.close()
        if self.spool is not None:
            self.spool.close()


def open_tree(source: str) -> Tree:
    if source == "-":
        spool = tempfile.TemporaryFile()
        shutil.copyfileobj(sys.stdin.buffer, spool)
        spool.seek(0)
        try:
            return TarTree(tarfile.open(fileobj=spool, mode="r:*"), "stdin", spool)
        except tarfile.TarError as exc:
            spool.close()
            raise InputError(f"stdin is not a tar archive ({exc}); pipe `docker export CONTAINER` into minisbom scan -")
    if os.path.isdir(source):
        return DirTree(source)
    if not os.path.exists(source):
        raise InputError(f"{source}: no such file or directory")
    try:
        return TarTree(tarfile.open(source, "r:*"), source)
    except tarfile.TarError as exc:
        raise InputError(f"{source}: not a directory or a tar archive ({exc}); export a filesystem with `docker export CONTAINER -o fs.tar`")


def looks_like_image_archive(tree: Tree) -> bool:
    paths = tree.paths()
    has_layers = any(p.endswith("/layer.tar") or p.startswith("blobs/sha256/") for p in paths)
    return "manifest.json" in paths and has_layers and tree.kind("etc")[0] != "dir"


@dataclass
class Distro:
    id: str
    version: str
    codename: str = ""
    pretty: str = ""

    @property
    def qualifier(self) -> str:
        if self.version:
            return f"{self.id}-{self.version}"
        return f"{self.id}-{self.codename}" if self.codename else self.id


def parse_os_release(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def read_distro(tree: Tree) -> Optional[Distro]:
    for path in OS_RELEASE_FILES:
        text = tree.text(path)
        if text:
            values = parse_os_release(text)
            if values.get("ID"):
                return Distro(values["ID"].lower(), values.get("VERSION_ID", ""), values.get("VERSION_CODENAME", ""), values.get("PRETTY_NAME", ""))
    return None


@dataclass
class Package:
    type: str
    name: str
    version: str
    namespace: str = ""
    arch: str = ""
    distro: str = ""
    source: str = ""
    source_version: str = ""
    licenses: List[str] = field(default_factory=list)
    locations: List[str] = field(default_factory=list)
    dev: bool = False
    files: int = 0
    modified: List[str] = field(default_factory=list)

    @property
    def display(self) -> str:
        return f"{self.namespace}/{self.name}" if self.type == "npm" and self.namespace else self.name

    def qualifiers(self) -> Dict[str, str]:
        if self.type not in ("deb", "apk"):
            return {}
        found = {"arch": self.arch, "distro": self.distro}
        if self.source and self.source_version and self.source_version != self.version:
            found["upstream"] = f"{self.source}@{self.source_version}"
        elif self.source and self.source != self.name:
            found["upstream"] = self.source
        return {key: value for key, value in found.items() if value}

    @property
    def purl(self) -> str:
        return make_purl(self.type, self.namespace, purl_name(self.type, self.name), self.version, self.qualifiers())


@dataclass
class Inventory:
    source: str
    distro: Optional[Distro]
    packages: List[Package]


def parse_stanzas(text: str, location: str) -> List[Dict[str, str]]:
    stanzas: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    last = ""
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            if current:
                stanzas.append(current)
            current, last = {}, ""
            continue
        if line[0] in " \t":
            if not last:
                raise InputError(f"{location}:{number}: a continuation line before any field")
            current[last] += "\n" + line.strip()
            continue
        key, colon, value = line.partition(":")
        if not colon:
            raise InputError(f"{location}:{number}: expected 'Field: value', got {line[:60]!r}")
        last = key.strip()
        current[last] = value.strip()
    if current:
        stanzas.append(current)
    return stanzas


def debian_licenses(tree: Tree, package: str) -> List[str]:
    text = tree.text(f"usr/share/doc/{package}/copyright")
    if not text or not text.lstrip().startswith("Format:"):
        return []
    found: List[str] = []
    for line in text.splitlines():
        if line.startswith("License:"):
            value = line[len("License:"):].strip()
            if value and value not in found:
                found.append(value)
    return found


def dpkg_packages(tree: Tree, distro: Optional[Distro]) -> List[Package]:
    locations = [DPKG_STATUS] + [p for p in tree.paths() if p.startswith(DPKG_STATUS_DIR) and not p.endswith(".md5sums")]
    namespace = distro.id if distro and distro.id in ("debian", "ubuntu") else "debian"
    found: List[Package] = []
    for location in locations:
        text = tree.text(location)
        if text is None:
            continue
        for stanza in parse_stanzas(text, location):
            name, version = stanza.get("Package", ""), stanza.get("Version", "")
            state = (stanza.get("Status") or "install ok installed").split()[-1]
            if not name or not version or state in DPKG_GONE_STATES:
                continue
            source, source_version = name, version
            match = SOURCE_FIELD.match(stanza.get("Source", ""))
            if match:
                source, source_version = match.group(1), match.group(2) or version
            found.append(Package(
                "deb", name, version, namespace=namespace, arch=stanza.get("Architecture", ""),
                distro=distro.qualifier if distro else "", source=source, source_version=source_version,
                licenses=debian_licenses(tree, name), locations=[location],
            ))
    return found


def parse_apk_db(text: str) -> List[Dict[str, List[str]]]:
    records: List[Dict[str, List[str]]] = []
    current: Dict[str, List[str]] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                records.append(current)
            current = {}
            continue
        if len(line) >= 2 and line[1] == ":":
            current.setdefault(line[0], []).append(line[2:])
    if current:
        records.append(current)
    return records


def first(record: Dict[str, List[str]], key: str) -> str:
    return (record.get(key) or [""])[0]


def apk_packages(tree: Tree, distro: Optional[Distro]) -> List[Package]:
    found: List[Package] = []
    seen = set()
    for location in APK_DATABASES:
        real = tree.resolve(location)
        text = None if real in seen else tree.text(location)
        if text is None:
            continue
        seen.add(real)
        for record in parse_apk_db(text):
            name, version = first(record, "P"), first(record, "V")
            if not name or not version:
                continue
            license_text = first(record, "L")
            found.append(Package(
                "apk", name, version, namespace=distro.id if distro else "alpine", arch=first(record, "A"),
                distro=distro.qualifier if distro else "", source=first(record, "o") or name, source_version=version,
                licenses=[license_text] if license_text else [], files=len(record.get("R", [])), locations=[location],
            ))
    return found


def python_licenses(headers: Message) -> List[str]:
    expression = (headers.get("License-Expression") or "").strip()
    if expression:
        return [expression]
    declared = (headers.get("License") or "").strip()
    if declared and "\n" not in declared and len(declared) <= 80 and declared.upper() != "UNKNOWN":
        return [declared]
    found: List[str] = []
    for classifier in headers.get_all("Classifier") or []:
        parts = [part.strip() for part in classifier.split("::")]
        if parts[0] == "License" and len(parts) > 1 and parts[-1] != "OSI Approved" and parts[-1] not in found:
            found.append(parts[-1])
    return found


def parse_record(text: str) -> List[Tuple[str, str]]:
    return [(row[0], row[1] if len(row) > 1 else "") for row in csv.reader(io.StringIO(text)) if row and row[0]]


def modified_files(tree: Tree, meta_dir: str, entries: Sequence[Tuple[str, str]]) -> List[str]:
    site = meta_dir.rsplit("/", 1)[0] if "/" in meta_dir else ""
    changed: List[str] = []
    for relative, digest in entries:
        algorithm, _, expected = digest.partition("=")
        if not expected or algorithm not in hashlib.algorithms_guaranteed:
            continue
        data = tree.read(f"{site}/{relative}" if site else relative)
        if data is None:
            changed.append(f"{relative} (missing)")
            continue
        actual = base64.urlsafe_b64encode(hashlib.new(algorithm, data).digest()).rstrip(b"=").decode("ascii")
        if actual != expected:
            changed.append(relative)
    return changed


def python_packages(tree: Tree, verify: bool) -> List[Package]:
    found: List[Package] = []
    for path in tree.paths():
        if not PYTHON_METADATA.search(path):
            continue
        text = tree.text(path)
        if text is None:
            continue
        headers = HeaderParser().parsestr(text, headersonly=True)
        name, version = (headers.get("Name") or "").strip(), (headers.get("Version") or "").strip()
        if not name or not version:
            continue
        meta_dir = path.rsplit("/", 1)[0]
        package = Package("pypi", name, version, licenses=python_licenses(headers), locations=[meta_dir])
        record = tree.text(meta_dir + "/RECORD") if path.endswith("/METADATA") else None
        if record is not None:
            entries = parse_record(record)
            package.files = len(entries)
            if verify:
                package.modified = modified_files(tree, meta_dir, entries)
        found.append(package)
    return found


def npm_package(name: str, version: str, license_value: Any, dev: bool, location: str) -> Package:
    namespace, short = "", name
    if name.startswith("@") and "/" in name:
        namespace, short = name.split("/", 1)
    if isinstance(license_value, dict):
        license_value = license_value.get("type")
    licenses = [license_value] if isinstance(license_value, str) and license_value else []
    return Package("npm", short, version, namespace=namespace, licenses=licenses, dev=dev, locations=[location])


def walk_npm_v1(dependencies: Any, location: str, found: List[Package]) -> None:
    for name, entry in sorted((dependencies or {}).items()):
        if isinstance(entry, dict) and entry.get("version"):
            found.append(npm_package(name, str(entry["version"]), None, bool(entry.get("dev")), location))
            walk_npm_v1(entry.get("dependencies"), location, found)


def parse_npm_lock(data: Any, location: str) -> List[Package]:
    if not isinstance(data, dict):
        raise InputError(f"{location}: expected a JSON object with lockfileVersion")
    found: List[Package] = []
    packages = data.get("packages")
    if isinstance(packages, dict):
        for key, entry in sorted(packages.items()):
            if not key or not isinstance(entry, dict) or entry.get("link") or not entry.get("version"):
                continue
            name = entry.get("name") or key.rsplit("node_modules/", 1)[-1]
            found.append(npm_package(name, str(entry["version"]), entry.get("license"), bool(entry.get("dev")), location))
        return found
    walk_npm_v1(data.get("dependencies"), location, found)
    return found


def npm_packages(tree: Tree) -> List[Package]:
    found: List[Package] = []
    for path in tree.paths():
        if path.rsplit("/", 1)[-1] not in NPM_LOCKS or "/node_modules/" in "/" + path:
            continue
        text = tree.text(path)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InputError(f"{path}: not valid JSON ({exc.msg} at line {exc.lineno})")
        found.extend(parse_npm_lock(data, path))
    return found


def merge(packages: Sequence[Package]) -> List[Package]:
    by_purl: Dict[str, Package] = {}
    for package in packages:
        existing = by_purl.get(package.purl)
        if existing is None:
            by_purl[package.purl] = package
        else:
            existing.locations.extend(loc for loc in package.locations if loc not in existing.locations)
    return sorted(by_purl.values(), key=lambda p: (TYPES.index(p.type), p.display.lower(), p.version))


def inventory(tree: Tree, verify: bool = False) -> Inventory:
    distro = read_distro(tree)
    packages = dpkg_packages(tree, distro) + apk_packages(tree, distro) + python_packages(tree, verify) + npm_packages(tree)
    return Inventory(tree.label, distro, merge(packages))


def spdx_id(token: str) -> Optional[str]:
    text = token.strip()
    return SPDX_BY_LOWER.get(text.lower()) or LICENSE_ALIASES.get(text.lower())


def spdx_expression(text: str) -> Optional[str]:
    tokens = re.findall(r"\(|\)|[^\s()]+", text)
    out: List[str] = []
    expect_operand = True
    for token in tokens:
        if token in ("(", ")"):
            out.append(token)
            continue
        if token.upper() in ("AND", "OR", "WITH"):
            if expect_operand:
                return None
            out.append(token.upper())
            expect_operand = True
            continue
        known = spdx_id(token)
        if known is None or not expect_operand:
            return None
        out.append(known)
        expect_operand = False
    if not out or expect_operand:
        return None
    return " ".join(out).replace("( ", "(").replace(" )", ")")


def declared_license(licenses: Sequence[str]) -> Optional[str]:
    expressions = [spdx_expression(text) for text in licenses]
    if not expressions or any(e is None for e in expressions):
        return None
    unique = list(dict.fromkeys(e for e in expressions if e))
    if len(unique) == 1:
        return unique[0]
    return " AND ".join(f"({e})" if " " in e else e for e in unique)


def cyclonedx_licenses(licenses: Sequence[str]) -> List[Dict[str, Any]]:
    if len(licenses) == 1:
        expression = spdx_expression(licenses[0])
        if expression and " " in expression:
            return [{"expression": expression}]
    found = []
    for text in licenses:
        known = spdx_expression(text)
        found.append({"license": {"id": known} if known and " " not in known else {"name": text}})
    return found


def document_time(explicit: Optional[str], environ: Mapping[str, str]) -> str:
    if explicit:
        try:
            datetime.strptime(explicit, TIME_FORMAT)
        except ValueError:
            raise InputError(f"--timestamp {explicit!r}: expected UTC in the form 2026-01-01T00:00:00Z")
        return explicit
    epoch = environ.get("SOURCE_DATE_EPOCH", "")
    if epoch:
        if not epoch.isdigit():
            raise InputError(f"SOURCE_DATE_EPOCH={epoch!r} is not a number of seconds")
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime(TIME_FORMAT)
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


def document_uuid(inv: Inventory, name: str, timestamp: str) -> str:
    seed = f"{TOOL}:{name}:{timestamp}:" + ",".join(sorted(p.purl for p in inv.packages))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def package_properties(package: Package) -> List[Dict[str, str]]:
    found = [{"name": "minisbom:ecosystem", "value": ECOSYSTEM_LABELS[package.type]}]
    found.extend({"name": "minisbom:location", "value": location} for location in package.locations)
    if package.source and package.source != package.name:
        found.append({"name": "minisbom:source", "value": package.source})
    if package.files:
        found.append({"name": "minisbom:files", "value": str(package.files)})
    found.extend({"name": "minisbom:modified", "value": path} for path in package.modified)
    if package.dev:
        found.append({"name": "minisbom:npm-dev", "value": "true"})
    return found


def cyclonedx(inv: Inventory, name: str, timestamp: str) -> Dict[str, Any]:
    components: List[Dict[str, Any]] = []
    if inv.distro:
        components.append({
            "type": "operating-system", "bom-ref": f"os:{inv.distro.qualifier}", "name": inv.distro.id,
            "version": inv.distro.version or inv.distro.codename, "description": inv.distro.pretty,
        })
    for package in inv.packages:
        item: Dict[str, Any] = {"type": "library", "bom-ref": package.purl, "name": package.display, "version": package.version, "purl": package.purl}
        licenses = cyclonedx_licenses(package.licenses)
        if licenses:
            item["licenses"] = licenses
        item["properties"] = package_properties(package)
        components.append(item)
    return {
        "$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": "urn:uuid:" + document_uuid(inv, name, timestamp),
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "tools": {"components": [{"type": "application", "name": TOOL, "version": TOOL_VERSION}]},
            "component": {"type": "container" if inv.distro else "application", "bom-ref": "subject", "name": name},
        },
        "components": components,
    }


def spdx_ref(text: str, used: Dict[str, int]) -> str:
    base = "SPDXRef-" + re.sub(r"[^A-Za-z0-9.-]+", "-", text).strip("-")
    used[base] = used.get(base, 0) + 1
    return base if used[base] == 1 else f"{base}-{used[base]}"


def spdx(inv: Inventory, name: str, timestamp: str) -> Dict[str, Any]:
    used: Dict[str, int] = {}
    subject = "SPDXRef-Subject"
    packages: List[Dict[str, Any]] = [{
        "name": name, "SPDXID": subject, "downloadLocation": "NOASSERTION", "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION", "licenseDeclared": "NOASSERTION", "copyrightText": "NOASSERTION",
        "primaryPackagePurpose": "CONTAINER" if inv.distro else "APPLICATION",
    }]
    relationships = [{"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": subject}]
    if inv.distro:
        os_ref = spdx_ref(f"OperatingSystem-{inv.distro.qualifier}", used)
        packages.append({
            "name": inv.distro.id, "SPDXID": os_ref, "versionInfo": inv.distro.version or inv.distro.codename,
            "downloadLocation": "NOASSERTION", "filesAnalyzed": False, "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION", "copyrightText": "NOASSERTION", "primaryPackagePurpose": "OPERATING-SYSTEM",
        })
        relationships.append({"spdxElementId": subject, "relationshipType": "CONTAINS", "relatedSpdxElement": os_ref})
    for package in inv.packages:
        ref = spdx_ref(f"Package-{package.type}-{package.display}-{package.version}", used)
        packages.append({
            "name": package.display, "SPDXID": ref, "versionInfo": package.version, "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False, "licenseConcluded": "NOASSERTION",
            "licenseDeclared": declared_license(package.licenses) or "NOASSERTION", "copyrightText": "NOASSERTION",
            "sourceInfo": "acquired package info from " + ", ".join(package.locations), "primaryPackagePurpose": "LIBRARY",
            "externalRefs": [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl", "referenceLocator": package.purl}],
        })
        relationships.append({"spdxElementId": subject, "relationshipType": "CONTAINS", "relatedSpdxElement": ref})
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": name,
        "documentNamespace": f"https://cloudcaptain.io/spdx/{TOOL}/{quote(name, safe='')}-{document_uuid(inv, name, timestamp)}",
        "creationInfo": {"created": timestamp, "creators": [f"Tool: {TOOL}-{TOOL_VERSION}"]},
        "packages": packages,
        "relationships": relationships,
    }


def inventory_json(inv: Inventory) -> Dict[str, Any]:
    counts = {kind: sum(1 for p in inv.packages if p.type == kind) for kind in TYPES}
    return {
        "tool": TOOL,
        "version": TOOL_VERSION,
        "source": inv.source,
        "distro": None if inv.distro is None else {"id": inv.distro.id, "version": inv.distro.version, "codename": inv.distro.codename, "pretty": inv.distro.pretty},
        "counts": {kind: count for kind, count in counts.items() if count},
        "packages": [
            {
                "type": p.type, "name": p.display, "version": p.version, "purl": p.purl, "arch": p.arch,
                "source": p.source, "source_version": p.source_version, "licenses": p.licenses,
                "locations": p.locations, "files": p.files, "modified": p.modified, "dev": p.dev,
            }
            for p in inv.packages
        ],
    }


@dataclass
class Component:
    type: str
    namespace: str
    name: str
    version: str
    qualifiers: Dict[str, str]
    purl: str

    @property
    def display(self) -> str:
        return f"{self.namespace}/{self.name}" if self.type == "npm" and self.namespace else self.name

    @property
    def key(self) -> Tuple[str, str, str]:
        return self.type, self.namespace.lower(), name_key(self.type, self.name)

    def upstream(self) -> Tuple[str, str]:
        text = self.qualifiers.get("upstream", "")
        name, _, version = text.partition("@")
        return name, version


def component_of(package: Package) -> Component:
    return Component(package.type, package.namespace, purl_name(package.type, package.name), package.version, package.qualifiers(), package.purl)


@dataclass
class Sbom:
    label: str
    components: List[Component]
    os: str = ""


def unwrap(data: Any) -> Any:
    if isinstance(data, dict) and "payload" in data and "payloadType" in data:
        try:
            data = json.loads(base64.b64decode(data["payload"]))
        except (ValueError, TypeError):
            raise InputError("a DSSE envelope whose payload is not base64-encoded JSON")
    if isinstance(data, dict) and str(data.get("_type", "")).startswith("https://in-toto.io/Statement") and isinstance(data.get("predicate"), dict):
        data = data["predicate"]
    return data


def flatten(items: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            found.append(item)
            found.extend(flatten(item.get("components")))
    return found


def sbom_from_document(data: Any, label: str) -> Sbom:
    data = unwrap(data)
    if not isinstance(data, dict):
        raise InputError(f"{label}: not a CycloneDX, SPDX or minisbom JSON document")
    entries: List[Tuple[str, str]] = []
    os_label = ""
    if data.get("bomFormat") == "CycloneDX":
        for item in flatten(data.get("components")):
            if item.get("type") == "operating-system":
                os_label = f"{item.get('name', '')} {item.get('version', '')}".strip()
            entries.append((item.get("purl") or "", str(item.get("version") or "")))
    elif str(data.get("spdxVersion", "")).startswith("SPDX-"):
        for item in data.get("packages") or []:
            if item.get("primaryPackagePurpose") == "OPERATING-SYSTEM":
                os_label = f"{item.get('name', '')} {item.get('versionInfo', '')}".strip()
            refs = [r.get("referenceLocator", "") for r in item.get("externalRefs") or [] if r.get("referenceType") == "purl"]
            entries.extend((ref, str(item.get("versionInfo") or "")) for ref in refs)
    elif data.get("tool") == TOOL:
        distro = data.get("distro") or {}
        os_label = f"{distro.get('id', '')} {distro.get('version', '')}".strip()
        entries = [(p.get("purl", ""), str(p.get("version", ""))) for p in data.get("packages") or []]
    else:
        raise InputError(f"{label}: not a CycloneDX, SPDX or minisbom JSON document (no bomFormat, spdxVersion or tool field)")
    components = []
    for purl_text, version in entries:
        if not purl_text.startswith("pkg:"):
            continue
        parsed = parse_purl(purl_text)
        components.append(Component(parsed.type, parsed.namespace, parsed.name, version or parsed.version, parsed.qualifiers, purl_text))
    return Sbom(label, components, os_label)


def read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise InputError(f"{path}: {exc.strerror}")
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: not valid JSON ({exc.msg} at line {exc.lineno})")


def load_sbom(path: str) -> Sbom:
    if path == "-" or os.path.isdir(path) or (os.path.isfile(path) and tarfile.is_tarfile(path)):
        tree = open_tree(path)
        try:
            inv = inventory(tree)
        finally:
            tree.close()
        os_label = f"{inv.distro.id} {inv.distro.version}".strip() if inv.distro else ""
        return Sbom(path, [component_of(p) for p in inv.packages], os_label)
    return sbom_from_document(read_json(path), path)


@dataclass
class Change:
    old: Component
    new: Component
    direction: str


@dataclass
class DiffResult:
    added: List[Component]
    removed: List[Component]
    changed: List[Change]
    os_change: Optional[Tuple[str, str]]

    @property
    def total(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed) + (1 if self.os_change else 0)


def direction(kind: str, old: str, new: str) -> str:
    try:
        order = compare_versions(kind, old, new)
    except VersionError:
        return "changed"
    return {-1: "upgrade", 0: "equivalent", 1: "downgrade"}[order]


def component_order(component: Component) -> Tuple[int, str, str]:
    return TYPES.index(component.type) if component.type in TYPES else len(TYPES), component.display.lower(), component.version


def diff(old: Sbom, new: Sbom) -> DiffResult:
    def group(components: Sequence[Component]) -> Dict[Tuple[str, str, str], Dict[str, Component]]:
        grouped: Dict[Tuple[str, str, str], Dict[str, Component]] = {}
        for component in components:
            grouped.setdefault(component.key, {})[component.version] = component
        return grouped

    before, after = group(old.components), group(new.components)
    added: List[Component] = []
    removed: List[Component] = []
    changed: List[Change] = []
    for key in sorted(set(before) | set(after)):
        was, now = before.get(key, {}), after.get(key, {})
        gone = [was[v] for v in sorted(set(was) - set(now))]
        came = [now[v] for v in sorted(set(now) - set(was))]
        if len(gone) == 1 and len(came) == 1:
            changed.append(Change(gone[0], came[0], direction(key[0], gone[0].version, came[0].version)))
        else:
            removed.extend(gone)
            added.extend(came)
    os_change = (old.os, new.os) if old.os and new.os and old.os != new.os else None
    return DiffResult(sorted(added, key=component_order), sorted(removed, key=component_order), sorted(changed, key=lambda c: component_order(c.new)), os_change)


def describe(component: Component) -> Dict[str, Any]:
    return {"type": component.type, "name": component.display, "version": component.version, "purl": component.purl}


def diff_json(result: DiffResult) -> Dict[str, Any]:
    return {
        "added": [describe(c) for c in result.added],
        "removed": [describe(c) for c in result.removed],
        "changed": [{"type": c.new.type, "name": c.new.display, "from": c.old.version, "to": c.new.version, "direction": c.direction} for c in result.changed],
        "os": None if result.os_change is None else {"from": result.os_change[0], "to": result.os_change[1]},
        "total": result.total,
    }


def component_ecosystem(component: Component) -> str:
    if component.type == "pypi":
        return "PyPI"
    if component.type == "npm":
        return "npm"
    if component.type not in ("deb", "apk"):
        return ""
    family, _, release = component.qualifiers.get("distro", "").partition("-")
    if not release and family in DEBIAN_CODENAMES:
        family, release = "debian", DEBIAN_CODENAMES[family]
    family = (component.namespace or family).lower()
    if component.type == "apk":
        parts = release.split(".")
        return f"Alpine:v{parts[0]}.{parts[1]}" if len(parts) >= 2 else "Alpine"
    if family == "ubuntu":
        return f"Ubuntu:{release}" if release else "Ubuntu"
    if release in DEBIAN_CODENAMES:
        release = DEBIAN_CODENAMES[release]
    return f"Debian:{release.split('.')[0]}" if release else "Debian"


def ecosystem_matches(advisory: str, component: str) -> bool:
    family_a, _, release_a = advisory.partition(":")
    family_c, _, release_c = component.partition(":")
    if family_a != family_c:
        return False
    return not release_a or not release_c or release_a.split(":")[0] == release_c


def event_version(event: Mapping[str, str]) -> Tuple[str, str]:
    for kind in ("introduced", "fixed", "last_affected", "limit"):
        if kind in event:
            return kind, str(event[kind])
    raise InputError(f"an OSV range event without introduced, fixed, last_affected or limit: {dict(event)}")


def in_range(version: str, events: Sequence[Mapping[str, str]], compare: Callable[[str, str], int]) -> bool:
    typed = [event_version(event) for event in events]

    def order(a: Tuple[str, str], b: Tuple[str, str]) -> int:
        if a[1] == b[1]:
            return 0
        if a[0] == "introduced" and a[1] == "0":
            return -1
        if b[0] == "introduced" and b[1] == "0":
            return 1
        return compare(a[1], b[1])

    limits = [value for kind, value in typed if kind == "limit" and value != "*"]
    if limits and not any(compare(version, limit) < 0 for limit in limits):
        return False
    affected = False
    for kind, value in sorted((e for e in typed if e[0] != "limit"), key=functools.cmp_to_key(order)):
        if kind == "introduced" and (value == "0" or compare(version, value) >= 0):
            affected = True
        elif kind == "fixed" and compare(version, value) >= 0:
            affected = False
        elif kind == "last_affected" and compare(version, value) > 0:
            affected = False
    return affected


def range_text(events: Sequence[Mapping[str, str]]) -> str:
    return ", ".join(f"{kind} {value}" for kind, value in (event_version(e) for e in events))


@dataclass
class Finding:
    advisory: str
    aliases: List[str]
    summary: str
    severity: str
    component: Component
    matched_name: str
    matched_version: str
    ecosystem: str
    reason: str
    fixed: List[str]


def affected_reason(kind: str, affected: Mapping[str, Any], version: str) -> Tuple[Optional[str], List[str]]:
    if version in (affected.get("versions") or []):
        return f"version {version} is listed in versions", []
    for rng in affected.get("ranges") or []:
        if rng.get("type") == "GIT":
            continue
        compare = compare_semver if rng.get("type") == "SEMVER" else COMPARATORS[kind]
        events = rng.get("events") or []
        if in_range(version, events, compare):
            fixed = [str(e["fixed"]) for e in events if "fixed" in e]
            return f"{rng.get('type')} range: {range_text(events)}", fixed
    return None, []


def headline(advisory: Mapping[str, Any], width: int = 160) -> str:
    text = str(advisory.get("summary") or advisory.get("details") or "").strip().split("\n")[0]
    return text if len(text) <= width else text[:width - 3].rsplit(" ", 1)[0] + "..."


def advisory_severity(advisory: Mapping[str, Any], affected: Mapping[str, Any]) -> str:
    for block in (advisory.get("database_specific"), affected.get("ecosystem_specific"), affected.get("database_specific")):
        if isinstance(block, dict):
            for key in ("severity", "urgency"):
                if isinstance(block.get(key), str) and block[key]:
                    return block[key].upper()
    for entry in advisory.get("severity") or []:
        if isinstance(entry, dict) and entry.get("score"):
            return str(entry["score"])
    return "UNKNOWN"


def load_advisories(path: str) -> List[Dict[str, Any]]:
    if os.path.isdir(path):
        files = sorted(os.path.join(path, name) for name in os.listdir(path) if name.endswith(".json"))
    elif os.path.isfile(path):
        files = [path]
    else:
        raise InputError(f"{path}: no such advisory file or directory")
    found: List[Dict[str, Any]] = []
    for name in files:
        data = read_json(name)
        if isinstance(data, dict) and isinstance(data.get("vulns"), list):
            found.extend(data["vulns"])
        elif isinstance(data, list):
            found.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict) and data.get("id") and "affected" in data:
            found.append(data)
        else:
            raise InputError(f"{name}: not an OSV record, a list of OSV records or an OSV query response")
    return found


def match(sbom: Sbom, advisories: Sequence[Mapping[str, Any]]) -> Tuple[List[Finding], List[str]]:
    index: Dict[Tuple[str, str], List[Tuple[Mapping[str, Any], Mapping[str, Any]]]] = {}
    for advisory in advisories:
        for affected in advisory.get("affected") or []:
            package = affected.get("package") or {}
            family = str(package.get("ecosystem", "")).split(":")[0]
            kind = OSV_FAMILIES.get(family)
            if kind:
                index.setdefault((family, name_key(kind, str(package.get("name", "")))), []).append((advisory, affected))
    findings: List[Finding] = []
    notes: List[str] = []
    seen = set()
    for component in sbom.components:
        ecosystem = component_ecosystem(component)
        if not ecosystem:
            continue
        family = ecosystem.split(":")[0]
        upstream_name, upstream_version = component.upstream()
        candidates = [(component.display, component.version)]
        if upstream_name:
            candidates.append((upstream_name, upstream_version or component.version))
        for name, version in candidates:
            for advisory, affected in index.get((family, name_key(component.type, name)), []):
                if not ecosystem_matches(str(affected["package"].get("ecosystem", "")), ecosystem):
                    continue
                marker = (advisory.get("id"), component.purl)
                if marker in seen:
                    continue
                try:
                    reason, fixed = affected_reason(component.type, affected, version)
                except VersionError as exc:
                    notes.append(f"{advisory.get('id')}: cannot compare {component.display} {version}: {exc}")
                    continue
                if reason is None:
                    continue
                seen.add(marker)
                findings.append(Finding(
                    str(advisory.get("id")), list(advisory.get("aliases") or []), headline(advisory),
                    advisory_severity(advisory, affected), component, name, version, ecosystem, reason, fixed,
                ))
    findings.sort(key=lambda f: (f.advisory, f.component.type, f.component.display))
    return findings, notes


def findings_json(sbom: Sbom, findings: Sequence[Finding], notes: Sequence[str], advisory_count: int) -> Dict[str, Any]:
    return {
        "checked": len(sbom.components),
        "advisories": advisory_count,
        "findings": [
            {
                "advisory": f.advisory, "aliases": f.aliases, "summary": f.summary, "severity": f.severity,
                "package": describe(f.component), "matched": {"name": f.matched_name, "version": f.matched_version, "ecosystem": f.ecosystem},
                "reason": f.reason, "fixed": f.fixed,
            }
            for f in findings
        ],
        "notes": list(notes),
    }


def table(rows: Sequence[Sequence[str]]) -> List[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows]


def render_inventory(inv: Inventory) -> str:
    about = f" ({inv.distro.pretty or inv.distro.qualifier})" if inv.distro else ""
    lines = [f"source {inv.source}{about}"]
    if inv.packages:
        rows = [("TYPE", "NAME", "VERSION", "SOURCE", "LICENCES")]
        for p in inv.packages:
            source = p.source if p.source and p.source != p.name else "-"
            licences = ", ".join(p.licenses) or "-"
            rows.append((p.type, p.display + (" (dev)" if p.dev else ""), p.version, source, licences[:48]))
        lines.extend(table(rows))
        for p in inv.packages:
            for path in p.modified:
                lines.append(f"MODIFIED {p.display} {p.version}: {path} does not match its RECORD hash")
    counts = ", ".join(f"{kind} {sum(1 for p in inv.packages if p.type == kind)}" for kind in TYPES if any(p.type == kind for p in inv.packages))
    lines.append(f"{len(inv.packages)} packages" + (f": {counts}" if counts else " (no dpkg, apk, Python or npm metadata found)"))
    return "\n".join(lines)


def render_diff(result: DiffResult) -> str:
    lines: List[str] = []
    if result.os_change:
        lines.append(f"os  {result.os_change[0]} -> {result.os_change[1]}")
    lines.extend(f"+ {c.type:<4}  {c.display} {c.version}" for c in result.added)
    lines.extend(f"- {c.type:<4}  {c.display} {c.version}" for c in result.removed)
    lines.extend(f"~ {c.new.type:<4}  {c.new.display} {c.old.version} -> {c.new.version} ({c.direction})" for c in result.changed)
    if result.total:
        lines.append(f"{result.total} changes: {len(result.added)} added, {len(result.removed)} removed, {len(result.changed)} changed")
    else:
        lines.append("no changes")
    return "\n".join(lines)


def render_findings(sbom: Sbom, findings: Sequence[Finding], notes: Sequence[str], advisory_count: int) -> str:
    lines: List[str] = []
    for f in findings:
        via = f" (source {f.matched_name} {f.matched_version})" if f.matched_name != f.component.display else ""
        fix = f"fixed in {', '.join(f.fixed)}" if f.fixed else "no fixed version"
        lines.append(f"{f.advisory}  {f.severity}  {f.component.type} {f.component.display} {f.component.version}{via}  {fix}")
        aliases = f"aliases {', '.join(f.aliases)}; " if f.aliases else ""
        lines.append(f"    {aliases}{f.ecosystem} {f.reason}")
        if f.summary:
            lines.append(f"    {f.summary}")
    lines.extend(f"note: {note}" for note in notes)
    lines.append(f"{len(findings)} finding(s) in {len(sbom.components)} packages checked against {advisory_count} advisories")
    return "\n".join(lines)


def emit(text: str, output: Optional[str]) -> None:
    if output:
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print(text)


def command_scan(args: argparse.Namespace) -> int:
    tree = open_tree(args.source)
    try:
        if looks_like_image_archive(tree):
            raise InputError(f"{args.source} is a `docker save` archive (a manifest and layer tarballs), not a filesystem; scan `docker export` of a container created from the image instead")
        inv = inventory(tree, verify=args.verify_records)
    finally:
        tree.close()
    name = args.name or ("stdin" if args.source == "-" else os.path.basename(os.path.normpath(args.source)))
    timestamp = document_time(args.timestamp, os.environ)
    fmt = "json" if args.json else args.format
    if fmt == "cyclonedx":
        text = json.dumps(cyclonedx(inv, name, timestamp), indent=2)
    elif fmt == "spdx":
        text = json.dumps(spdx(inv, name, timestamp), indent=2)
    elif fmt == "json":
        text = json.dumps(inventory_json(inv), indent=2)
    else:
        text = render_inventory(inv)
    emit(text, args.output)
    if args.output:
        print(f"wrote {fmt} for {len(inv.packages)} packages to {args.output}", file=sys.stderr)
    return 1 if any(p.modified for p in inv.packages) else 0


def command_diff(args: argparse.Namespace) -> int:
    result = diff(load_sbom(args.old), load_sbom(args.new))
    print(json.dumps(diff_json(result), indent=2) if args.json else render_diff(result))
    return 1 if result.total else 0


def command_match(args: argparse.Namespace) -> int:
    sbom = load_sbom(args.sbom)
    advisories = load_advisories(args.advisories)
    findings, notes = match(sbom, advisories)
    if args.json:
        print(json.dumps(findings_json(sbom, findings, notes, len(advisories)), indent=2))
    else:
        print(render_findings(sbom, findings, notes, len(advisories)))
    return 1 if findings else 0


def command_compare(args: argparse.Namespace) -> int:
    symbol = {-1: "<", 0: "=", 1: ">"}[compare_versions(args.type, args.a, args.b)]
    print(json.dumps({"type": args.type, "a": args.a, "b": args.b, "result": symbol}) if args.json else symbol)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL, description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="inventory a directory, a tar archive (docker export) or - (a tar on stdin)")
    scan.add_argument("source")
    scan.add_argument("--format", choices=("table", "cyclonedx", "spdx", "json"), default="table")
    scan.add_argument("--json", action="store_true", help="same as --format json")
    scan.add_argument("--name", help="the subject name written into the SBOM (default: the source's base name)")
    scan.add_argument("--output", "-o", help="write the document to this file instead of stdout")
    scan.add_argument("--timestamp", help="document time as 2026-01-01T00:00:00Z (default: SOURCE_DATE_EPOCH, else now)")
    scan.add_argument("--verify-records", action="store_true", help="hash every file listed in Python RECORD files; exit 1 if any differs")
    scan.set_defaults(handler=command_scan)
    diff_parser = sub.add_parser("diff", help="compare two SBOMs (CycloneDX, SPDX, minisbom JSON) or two filesystems")
    diff_parser.add_argument("old")
    diff_parser.add_argument("new")
    diff_parser.add_argument("--json", action="store_true")
    diff_parser.set_defaults(handler=command_diff)
    match_parser = sub.add_parser("match", help="match an SBOM or a filesystem against OSV advisories")
    match_parser.add_argument("sbom")
    match_parser.add_argument("--advisories", required=True, help="an OSV JSON file, a list of records, an OSV query response or a directory of *.json")
    match_parser.add_argument("--json", action="store_true")
    match_parser.set_defaults(handler=command_match)
    compare = sub.add_parser("compare", help="compare two versions with an ecosystem's rules; prints <, = or >")
    compare.add_argument("type", choices=TYPES)
    compare.add_argument("a")
    compare.add_argument("b")
    compare.add_argument("--json", action="store_true")
    compare.set_defaults(handler=command_compare)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return args.handler(args)
    except InputError as exc:
        print(f"{TOOL}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
