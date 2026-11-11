from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from email.parser import HeaderParser
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence, Tuple

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
GENERATOR = "shipit 1.0.0"
METADATA_VERSION = "2.5"
PURE_TAG = "py3-none-any"
ZIP_EPOCH_MIN = 315532800
COMMAND_TIMEOUT = 30
README_TYPES = {".md": "text/markdown", ".rst": "text/x-rst", ".txt": "text/plain"}
KNOWN_PROJECT_KEYS = {
    "name", "version", "dynamic", "description", "readme", "requires-python", "dependencies", "license",
    "authors", "keywords", "classifiers", "urls", "scripts",
}


class ShipitError(Exception):
    code = EXIT_FAIL


class ConfigError(ShipitError):
    code = EXIT_USAGE


@dataclass
class CommandResult:
    code: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str], Path], CommandResult]


def run_command(args: Sequence[str], cwd: Path) -> CommandResult:
    try:
        done = subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True, timeout=COMMAND_TIMEOUT)
    except FileNotFoundError:
        return CommandResult(127, "", f"{args[0]}: command not found")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"{' '.join(args)}: timed out after {COMMAND_TIMEOUT}s")
    return CommandResult(done.returncode, done.stdout, done.stderr)


class TomlReader:
    def __init__(self, text: str, source: str):
        self.text = text.replace("\r\n", "\n")
        self.source = source
        self.pos = 0

    def line(self) -> int:
        return self.text.count("\n", 0, self.pos) + 1

    def fail(self, message: str) -> ConfigError:
        return ConfigError(f"{self.source}:{self.line()}: {message}")

    def peek(self, size: int = 1) -> str:
        return self.text[self.pos:self.pos + size]

    def skip_spaces(self) -> None:
        while self.peek() in (" ", "\t"):
            self.pos += 1

    def skip_comment(self) -> None:
        if self.peek() == "#":
            end = self.text.find("\n", self.pos)
            self.pos = len(self.text) if end < 0 else end

    def skip_blank(self) -> None:
        while self.pos < len(self.text):
            self.skip_spaces()
            self.skip_comment()
            if self.peek() == "\n":
                self.pos += 1
            else:
                return

    def end_of_line(self) -> None:
        self.skip_spaces()
        self.skip_comment()
        if self.pos < len(self.text) and self.peek() != "\n":
            raise self.fail(f"unexpected text after value: {self.text[self.pos:].splitlines()[0]!r}")

    def parse(self) -> dict:
        root: dict = {}
        current = root
        while True:
            self.skip_blank()
            if self.pos >= len(self.text):
                return root
            if self.peek(2) == "[[":
                self.pos += 2
                path = self.key_path("]]")
                parent = self.table_at(root, path[:-1])
                items = parent.setdefault(path[-1], [])
                if not isinstance(items, list):
                    raise self.fail(f"{'.'.join(path)} is not an array of tables")
                current = {}
                items.append(current)
            elif self.peek() == "[":
                self.pos += 1
                path = self.key_path("]")
                current = self.table_at(root, path)
            else:
                path = self.key_path("=")
                value = self.value()
                target = self.table_at(current, path[:-1])
                if path[-1] in target:
                    raise self.fail(f"duplicate key {'.'.join(path)}")
                target[path[-1]] = value
            self.end_of_line()

    def table_at(self, root: dict, path: List[str]) -> dict:
        table = root
        for part in path:
            table = table.setdefault(part, {})
            if isinstance(table, list):
                table = table[-1]
            if not isinstance(table, dict):
                raise self.fail(f"{'.'.join(path)} is not a table")
        return table

    def key_path(self, terminator: str) -> List[str]:
        parts = []
        while True:
            self.skip_spaces()
            if self.peek() in ('"', "'"):
                parts.append(self.string())
            else:
                match = re.compile(r"[A-Za-z0-9_-]+").match(self.text, self.pos)
                if not match:
                    raise self.fail(f"expected a key, got {self.peek()!r}")
                parts.append(match.group(0))
                self.pos = match.end()
            self.skip_spaces()
            if self.peek() == ".":
                self.pos += 1
                continue
            if self.peek(len(terminator)) != terminator:
                raise self.fail(f"expected {terminator!r} after key {'.'.join(parts)}")
            self.pos += len(terminator)
            return parts

    def string(self) -> str:
        quote = self.peek()
        if self.peek(3) == quote * 3:
            raise self.fail("multi-line strings are not supported by shipit's TOML subset")
        self.pos += 1
        out = []
        escapes = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "\r"}
        while True:
            char = self.peek()
            if char in ("", "\n"):
                raise self.fail("unterminated string")
            self.pos += 1
            if char == quote:
                return "".join(out)
            if char == "\\" and quote == '"':
                code = self.peek()
                self.pos += 1
                if code == "u":
                    out.append(chr(int(self.text[self.pos:self.pos + 4], 16)))
                    self.pos += 4
                elif code in escapes:
                    out.append(escapes[code])
                else:
                    raise self.fail(f"unknown escape \\{code}")
            else:
                out.append(char)

    def value(self):
        self.skip_spaces()
        char = self.peek()
        if char in ('"', "'"):
            return self.string()
        if char == "[":
            return self.array()
        if char == "{":
            return self.inline_table()
        for word, result in (("true", True), ("false", False)):
            if self.text.startswith(word, self.pos):
                self.pos += len(word)
                return result
        match = re.compile(r"[+-]?\d[\d_]*(?![.\deE:-])").match(self.text, self.pos)
        if match:
            self.pos = match.end()
            return int(match.group(0).replace("_", ""))
        raise self.fail(f"unsupported value starting with {self.text[self.pos:self.pos + 12]!r}")

    def array(self) -> list:
        self.pos += 1
        items = []
        while True:
            self.skip_blank()
            if self.peek() == "]":
                self.pos += 1
                return items
            items.append(self.value())
            self.skip_blank()
            if self.peek() == ",":
                self.pos += 1
            elif self.peek() != "]":
                raise self.fail("expected ',' or ']' in array")

    def inline_table(self) -> dict:
        self.pos += 1
        table: dict = {}
        self.skip_spaces()
        if self.peek() == "}":
            self.pos += 1
            return table
        while True:
            path = self.key_path("=")
            self.table_at(table, path[:-1])[path[-1]] = self.value()
            self.skip_spaces()
            if self.peek() == ",":
                self.pos += 1
            elif self.peek() == "}":
                self.pos += 1
                return table
            else:
                raise self.fail("expected ',' or '}' in inline table")


def parse_toml(text: str, source: str = "pyproject.toml") -> dict:
    return TomlReader(text, source).parse()


VERSION_RE = re.compile(
    r"^v?(?:(?P<epoch>\d+)!)?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre_l>a|b|rc|alpha|beta|c)[-_.]?(?P<pre_n>\d*))?"
    r"(?:[-_.]?(?:post|rev|r)[-_.]?(?P<post_n>\d*)|-(?P<post_implicit>\d+))?"
    r"(?:[-_.]?dev[-_.]?(?P<dev_n>\d*))?"
    r"(?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?$",
    re.IGNORECASE,
)
PRE_NAMES = {"a": "a", "alpha": "a", "b": "b", "beta": "b", "c": "rc", "rc": "rc"}
PRE_RANK = {"a": 0, "b": 1, "rc": 2}


@dataclass(frozen=True)
class Version:
    epoch: int
    release: Tuple[int, ...]
    pre: Optional[Tuple[str, int]] = None
    post: Optional[int] = None
    dev: Optional[int] = None
    local: Optional[str] = None

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None or self.dev is not None

    def public(self) -> str:
        text = f"{self.epoch}!" if self.epoch else ""
        text += ".".join(str(part) for part in self.release)
        if self.pre:
            text += f"{self.pre[0]}{self.pre[1]}"
        if self.post is not None:
            text += f".post{self.post}"
        if self.dev is not None:
            text += f".dev{self.dev}"
        return text

    def __str__(self) -> str:
        return self.public() + (f"+{self.local}" if self.local else "")

    @property
    def key(self) -> tuple:
        release = list(self.release)
        while len(release) > 1 and release[-1] == 0:
            release.pop()
        if self.pre is None and self.post is None and self.dev is not None:
            pre_key: tuple = (0,)
        elif self.pre is None:
            pre_key = (3,)
        else:
            pre_key = (1, PRE_RANK[self.pre[0]], self.pre[1])
        post_key = (0,) if self.post is None else (1, self.post)
        dev_key = (1,) if self.dev is None else (0, self.dev)
        return (self.epoch, tuple(release), pre_key, post_key, dev_key)


def parse_version(text: str) -> Version:
    match = VERSION_RE.match(text.strip())
    if not match:
        raise ShipitError(f"not a PEP 440 version: {text!r}")
    groups = match.groupdict()
    pre = None
    if groups["pre_l"]:
        pre = (PRE_NAMES[groups["pre_l"].lower()], int(groups["pre_n"] or 0))
    post = None
    if groups["post_implicit"] is not None:
        post = int(groups["post_implicit"])
    elif groups["post_n"] is not None:
        post = int(groups["post_n"] or 0)
    dev = int(groups["dev_n"] or 0) if groups["dev_n"] is not None else None
    local = re.sub(r"[-_]", ".", groups["local"].lower()) if groups["local"] else None
    return Version(int(groups["epoch"] or 0), tuple(int(part) for part in groups["release"].split(".")), pre, post, dev, local)


def normalize_version(text: str) -> str:
    return str(parse_version(text))


SPEC_RE = re.compile(r"^\s*(~=|==|!=|<=|>=|<|>)\s*([^\s,]+)\s*$")


def specifier_matches(op: str, spec: str, candidate: Version) -> bool:
    if op in ("==", "!=") and spec.endswith(".*"):
        prefix = parse_version(spec[:-2]).release
        padded = candidate.release + (0,) * max(0, len(prefix) - len(candidate.release))
        same = padded[:len(prefix)] == prefix
        return same if op == "==" else not same
    target = parse_version(spec)
    left = candidate.key
    right = target.key
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == ">=":
        return left >= right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    if len(target.release) < 2:
        raise ShipitError(f"~={spec} needs at least two release segments")
    prefix = ".".join(str(part) for part in target.release[:-1])
    return left >= right and specifier_matches("==", prefix + ".*", candidate)


@dataclass
class Requirement:
    name: str
    specifiers: List[Tuple[str, str]]
    text: str

    @property
    def key(self) -> str:
        return normalize_name(self.name)

    def allows(self, candidate: Version) -> bool:
        if candidate.is_prerelease and not any(parse_version(spec.rstrip(".*")).is_prerelease for _, spec in self.specifiers):
            return False
        return all(specifier_matches(op, spec, candidate) for op, spec in self.specifiers)


def parse_requirement(text: str) -> Requirement:
    if ";" in text:
        raise ShipitError(f"environment markers are not supported by shipit lock: {text!r}")
    match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[.*\])?\s*\(?([^)]*)\)?\s*$", text)
    if not match:
        raise ShipitError(f"not a requirement: {text!r}")
    if match.group(2):
        raise ShipitError(f"extras are not supported by shipit lock: {text!r}")
    specifiers = []
    for part in filter(None, (piece.strip() for piece in match.group(3).split(","))):
        spec = SPEC_RE.match(part)
        if not spec:
            raise ShipitError(f"bad version specifier {part!r} in {text!r}")
        specifiers.append((spec.group(1), spec.group(2)))
    return Requirement(match.group(1), specifiers, text.strip())


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def escape_name(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def wheel_filename(name: str, version: str, tag: str = PURE_TAG) -> str:
    return f"{escape_name(name)}-{version.replace('-', '_')}-{tag}.whl"


def dist_info_dir(name: str, version: str) -> str:
    return f"{escape_name(name)}-{version.replace('-', '_')}.dist-info"


def parse_wheel_filename(filename: str) -> Tuple[str, str, str]:
    match = re.match(r"^([^-]+)-([^-]+)(?:-\d[^-]*)?-([^-]+-[^-]+-[^-]+)\.whl$", Path(filename).name)
    if not match:
        raise ShipitError(f"not a wheel file name: {filename}")
    return match.group(1), match.group(2), match.group(3)


def record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Project:
    name: str
    version: Optional[str]
    summary: str = ""
    readme: str = ""
    readme_type: str = ""
    requires_python: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    license: Optional[str] = None
    authors: List[Dict[str, str]] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    classifiers: List[str] = field(default_factory=list)
    urls: Dict[str, str] = field(default_factory=dict)
    scripts: Dict[str, str] = field(default_factory=dict)
    version_source: str = "static"
    root: Optional[Path] = None
    package_root: Optional[Path] = None
    module: str = ""
    import_names: List[str] = field(default_factory=list)
    ignored: List[str] = field(default_factory=list)


def expect_type(value, kind, where: str):
    if not isinstance(value, kind):
        raise ConfigError(f"pyproject.toml: {where} must be a {kind.__name__}, got {type(value).__name__}")
    return value


def string_list(value, where: str) -> List[str]:
    expect_type(value, list, where)
    for item in value:
        expect_type(item, str, where + "[]")
    return list(value)


def find_module(root: Path, module: str) -> Tuple[Path, Path]:
    for base in (root / "src", root):
        if (base / module / "__init__.py").is_file():
            return base, base / module
        if (base / f"{module}.py").is_file():
            return base, base / f"{module}.py"
    raise ConfigError(f"cannot find the import package {module!r}: looked for src/{module}/__init__.py, {module}/__init__.py and {module}.py under {root}")


def load_project(root: Path) -> Project:
    path = root / "pyproject.toml"
    if not path.is_file():
        raise ConfigError(f"no pyproject.toml in {root}")
    data = parse_toml(path.read_text(encoding="utf-8"), str(path))
    table = expect_type(data.get("project"), dict, "[project]") if "project" in data else None
    if table is None:
        raise ConfigError(f"{path}: missing [project] table (PEP 621)")
    name = table.get("name")
    if not isinstance(name, str) or not re.match(r"^([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])$", name):
        raise ConfigError(f"{path}: project.name must be a valid distribution name, got {name!r}")
    dynamic = string_list(table.get("dynamic", []), "project.dynamic")
    tool = data.get("tool", {}).get("shipit", {})
    version = table.get("version")
    if version is not None and "version" in dynamic:
        raise ConfigError(f"{path}: project.version is set and also listed in project.dynamic; choose one")
    if version is None and "version" not in dynamic:
        raise ConfigError(f"{path}: set project.version or list \"version\" in project.dynamic")
    if version is not None:
        version = normalize_version(expect_type(version, str, "project.version"))
    license_value = table.get("license")
    if isinstance(license_value, dict):
        raise ConfigError(f"{path}: project.license must be an SPDX expression string such as \"MIT\" (PEP 639); the table form is deprecated")
    readme_text, readme_type = "", ""
    if "readme" in table:
        readme_path = root / expect_type(table["readme"], str, "project.readme")
        if not readme_path.is_file():
            raise ConfigError(f"{path}: project.readme points to {readme_path.name}, which does not exist")
        readme_text = readme_path.read_text(encoding="utf-8")
        readme_type = README_TYPES.get(readme_path.suffix.lower(), "text/plain")
    module = tool.get("module") or escape_name(name)
    package_root, _ = find_module(root, module)
    project = Project(
        name=name,
        version=version,
        summary=expect_type(table.get("description", ""), str, "project.description"),
        readme=readme_text,
        readme_type=readme_type,
        requires_python=table.get("requires-python"),
        dependencies=string_list(table.get("dependencies", []), "project.dependencies"),
        license=license_value,
        authors=[expect_type(author, dict, "project.authors[]") for author in expect_type(table.get("authors", []), list, "project.authors")],
        keywords=string_list(table.get("keywords", []), "project.keywords"),
        classifiers=string_list(table.get("classifiers", []), "project.classifiers"),
        urls=dict(expect_type(table.get("urls", {}), dict, "project.urls")),
        scripts=dict(expect_type(table.get("scripts", {}), dict, "project.scripts")),
        version_source=tool.get("version-source", "static" if version else "module"),
        root=root,
        package_root=package_root,
        module=module,
        import_names=[module],
        ignored=sorted(f"project.{key}" for key in table if key not in KNOWN_PROJECT_KEYS),
    )
    for requirement in project.dependencies:
        parse_requirement(requirement)
    if project.version_source not in ("static", "module", "git"):
        raise ConfigError(f"{path}: tool.shipit.version-source must be static, module or git")
    return project


def module_version(project: Project) -> Optional[str]:
    base = project.package_root / project.module
    source = base / "__init__.py" if base.is_dir() else project.package_root / f"{project.module}.py"
    match = re.search(r"^__version__\s*=\s*[\"']([^\"']+)[\"']", source.read_text(encoding="utf-8"), re.MULTILINE)
    return normalize_version(match.group(1)) if match else None


@dataclass
class GitVersion:
    version: str
    tag: Optional[str]
    distance: int
    commit: str
    dirty: bool


def next_dev_version(tag_version: Version, distance: int) -> str:
    if tag_version.pre:
        base = Version(tag_version.epoch, tag_version.release, (tag_version.pre[0], tag_version.pre[1] + 1))
    else:
        base = Version(tag_version.epoch, tag_version.release[:-1] + (tag_version.release[-1] + 1,))
    return f"{base.public()}.dev{distance}"


def version_from_git(root: Path, runner: Runner = run_command) -> GitVersion:
    described = runner(["git", "describe", "--tags", "--long", "--dirty", "--abbrev=7", "--match", "v[0-9]*"], root)
    if described.code == 0:
        match = re.match(r"^(?P<tag>.+)-(?P<distance>\d+)-g(?P<sha>[0-9a-f]+)(?P<dirty>-dirty)?$", described.stdout.strip())
        if not match:
            raise ShipitError(f"cannot read git describe output: {described.stdout.strip()!r}")
        tag = match.group("tag")
        tag_version = parse_version(tag)
        distance = int(match.group("distance"))
        dirty = bool(match.group("dirty"))
        sha = match.group("sha")
        if distance == 0 and not dirty:
            return GitVersion(str(tag_version), tag, 0, sha, False)
        local = f"g{sha}" + (".dirty" if dirty else "")
        return GitVersion(f"{next_dev_version(tag_version, distance)}+{local}", tag, distance, sha, dirty)
    count = runner(["git", "rev-list", "--count", "HEAD"], root)
    if count.code != 0:
        raise ConfigError(f"cannot derive a version from git in {root}: {(described.stderr or count.stderr).strip()}")
    sha = runner(["git", "rev-parse", "--short=7", "HEAD"], root).stdout.strip()
    status = runner(["git", "status", "--porcelain", "--untracked-files=no"], root)
    dirty = bool(status.stdout.strip())
    local = f"g{sha}" + (".dirty" if dirty else "")
    return GitVersion(f"0.1.dev{int(count.stdout.strip())}+{local}", None, int(count.stdout.strip()), sha, dirty)


def resolve_version(project: Project, runner: Runner = run_command) -> str:
    if project.version:
        return project.version
    if project.version_source == "git":
        return version_from_git(project.root, runner).version
    found = module_version(project)
    if not found:
        raise ConfigError(f"version is dynamic but {project.module} has no __version__ = \"...\" line; set tool.shipit.version-source = \"git\" or project.version")
    return found


def source_date_epoch(env: Dict[str, str], root: Optional[Path], runner: Runner = run_command) -> int:
    value = env.get("SOURCE_DATE_EPOCH")
    if value:
        if not value.isdigit():
            raise ConfigError(f"SOURCE_DATE_EPOCH must be a whole number of seconds, got {value!r}")
        return int(value)
    if root is not None:
        last = runner(["git", "log", "-1", "--format=%ct"], root)
        if last.code == 0 and last.stdout.strip().isdigit():
            return int(last.stdout.strip())
    return ZIP_EPOCH_MIN


def zip_info(name: str, epoch: int, mode: int = 0o644) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=time.gmtime(max(epoch, ZIP_EPOCH_MIN))[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def write_zip(target, entries: List[Tuple[str, bytes]], epoch: int) -> None:
    with zipfile.ZipFile(target, "w") as archive:
        for name, data in entries:
            archive.writestr(zip_info(name, epoch), data, compresslevel=6)


def metadata_text(project: Project, version: str) -> str:
    lines = [f"Metadata-Version: {METADATA_VERSION}", f"Name: {project.name}", f"Version: {version}"]
    if project.summary:
        lines.append(f"Summary: {project.summary}")
    if project.keywords:
        lines.append("Keywords: " + ",".join(project.keywords))
    named = [author for author in project.authors if "email" not in author and "name" in author]
    if named:
        lines.append("Author: " + ", ".join(author["name"] for author in named))
    emails = [f"{author['name']} <{author['email']}>" if "name" in author else author["email"] for author in project.authors if "email" in author]
    if emails:
        lines.append("Author-email: " + ", ".join(emails))
    if project.license:
        lines.append(f"License-Expression: {project.license}")
    lines += [f"Classifier: {classifier}" for classifier in project.classifiers]
    if project.requires_python:
        lines.append(f"Requires-Python: {project.requires_python}")
    lines += [f"Requires-Dist: {requirement}" for requirement in project.dependencies]
    lines += [f"Project-URL: {label}, {url}" for label, url in project.urls.items()]
    lines += [f"Import-Name: {name}" for name in project.import_names]
    if project.readme:
        lines.append(f"Description-Content-Type: {project.readme_type}")
        return "\n".join(lines) + "\n\n" + project.readme
    return "\n".join(lines) + "\n"


def wheel_text(tag: str = PURE_TAG) -> str:
    return f"Wheel-Version: 1.0\nGenerator: {GENERATOR}\nRoot-Is-Purelib: true\nTag: {tag}\n"


def entry_points_text(scripts: Dict[str, str]) -> str:
    return "[console_scripts]\n" + "".join(f"{name} = {target}\n" for name, target in sorted(scripts.items()))


def record_text(entries: List[Tuple[str, bytes]], record_path: str) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    for name, data in entries:
        writer.writerow([name, record_hash(data), len(data)])
    writer.writerow([record_path, "", ""])
    return out.getvalue()


def package_files(project: Project) -> List[Tuple[str, bytes]]:
    start = project.package_root / project.module
    paths = [project.package_root / f"{project.module}.py"] if not start.is_dir() else [
        path for path in start.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo") and not path.name.startswith(".")
    ]
    files = [(PurePosixPath(*path.relative_to(project.package_root).parts).as_posix(), path.read_bytes()) for path in paths]
    return sorted(files)


def wheel_entries(project: Project, version: str, files: List[Tuple[str, bytes]]) -> List[Tuple[str, bytes]]:
    info = dist_info_dir(project.name, version)
    entries = sorted(files)
    entries.append((f"{info}/METADATA", metadata_text(project, version).encode("utf-8")))
    entries.append((f"{info}/WHEEL", wheel_text().encode("utf-8")))
    if project.scripts:
        entries.append((f"{info}/entry_points.txt", entry_points_text(project.scripts).encode("utf-8")))
    record_path = f"{info}/RECORD"
    entries.append((record_path, record_text(entries, record_path).encode("utf-8")))
    return entries


@dataclass
class BuildResult:
    path: Path
    name: str
    version: str
    sha256: str
    files: List[str]
    epoch: int


def build_wheel(project: Project, version: str, out_dir: Path, epoch: int, files: Optional[List[Tuple[str, bytes]]] = None, dry_run: bool = False) -> BuildResult:
    entries = wheel_entries(project, version, package_files(project) if files is None else files)
    buffer = io.BytesIO()
    write_zip(buffer, entries, epoch)
    data = buffer.getvalue()
    path = out_dir / wheel_filename(project.name, version)
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return BuildResult(path, project.name, version, hashlib.sha256(data).hexdigest(), [name for name, _ in entries], epoch)


@dataclass
class WheelInfo:
    path: Path
    name: str
    version: str
    tag: str
    dist_info: str
    metadata: Dict[str, List[str]]
    requires: List[str]
    record: Dict[str, Tuple[str, str]]
    files: List[str]
    problems: List[str]
    scripts: Dict[str, str]


def unsafe_path(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return name.startswith("/") or "\\" in name or ".." in parts or (len(parts) > 0 and ":" in parts[0])


def parse_metadata(text: str) -> Dict[str, List[str]]:
    message = HeaderParser().parsestr(text)
    fields: Dict[str, List[str]] = {}
    for key, value in message.items():
        fields.setdefault(key, []).append(value)
    body = message.get_payload()
    if body:
        fields["Description"] = [body]
    return fields


def parse_entry_points(text: str) -> Dict[str, str]:
    scripts, section = {}, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif "=" in line and section == "console_scripts":
            key, _, value = line.partition("=")
            scripts[key.strip()] = value.strip()
    return scripts


def read_wheel(path: Path) -> WheelInfo:
    name, version, tag = parse_wheel_filename(path.name)
    problems: List[str] = []
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as error:
        raise ShipitError(f"{path.name}: not a readable zip archive ({error})")
    with archive:
        names = archive.namelist()
        dist_infos = sorted({entry.split("/")[0] for entry in names if entry.split("/")[0].endswith(".dist-info")})
        expected = f"{name}-{version}.dist-info"
        if dist_infos != [expected]:
            raise ShipitError(f"{path.name}: expected exactly one {expected}/ directory, found {dist_infos}")
        for entry in names:
            if unsafe_path(entry):
                problems.append(f"unsafe path {entry}")
        record_rows: Dict[str, Tuple[str, str]] = {}
        record_name = f"{expected}/RECORD"
        if record_name not in names:
            raise ShipitError(f"{path.name}: missing {record_name}")
        for row in csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))):
            if row:
                record_rows[row[0]] = (row[1] if len(row) > 1 else "", row[2] if len(row) > 2 else "")
        for entry in names:
            if entry.endswith("/") or entry in (record_name, f"{expected}/RECORD.jws", f"{expected}/RECORD.p7s"):
                continue
            data = archive.read(entry)
            if entry not in record_rows:
                problems.append(f"{entry} is not listed in RECORD")
                continue
            digest, size = record_rows[entry]
            if digest != record_hash(data):
                problems.append(f"{entry}: hash {record_hash(data)} does not match RECORD {digest}")
            elif size and int(size) != len(data):
                problems.append(f"{entry}: size {len(data)} does not match RECORD {size}")
        for entry in record_rows:
            if entry not in names:
                problems.append(f"RECORD lists {entry}, which is not in the archive")
        wheel_fields = parse_metadata(archive.read(f"{expected}/WHEEL").decode("utf-8")) if f"{expected}/WHEEL" in names else {}
        if not wheel_fields.get("Wheel-Version", [""])[0].startswith("1."):
            problems.append("WHEEL is missing or does not declare Wheel-Version 1.x")
        metadata = parse_metadata(archive.read(f"{expected}/METADATA").decode("utf-8")) if f"{expected}/METADATA" in names else {}
        if not metadata:
            problems.append("METADATA is missing")
        elif normalize_name(metadata.get("Name", [""])[0]) != normalize_name(name) or metadata.get("Version", [""])[0] != version:
            problems.append(f"METADATA says {metadata.get('Name')} {metadata.get('Version')}, file name says {name} {version}")
        entry_points = f"{expected}/entry_points.txt"
        scripts = parse_entry_points(archive.read(entry_points).decode("utf-8")) if entry_points in names else {}
    return WheelInfo(path, name, version, tag, expected, metadata, metadata.get("Requires-Dist", []), record_rows, names, problems, scripts)


@dataclass
class Candidate:
    name: str
    version: Version
    path: Path
    sha256: str
    requires: List[str]


def scan_index(directory: Path) -> Dict[str, List[Candidate]]:
    if not directory.is_dir():
        raise ConfigError(f"find-links directory {directory} does not exist")
    index: Dict[str, List[Candidate]] = {}
    for path in sorted(directory.glob("*.whl")):
        info = read_wheel(path)
        if info.tag != PURE_TAG:
            continue
        candidate = Candidate(info.metadata.get("Name", [info.name])[0], parse_version(info.version), path, file_sha256(path), info.requires)
        index.setdefault(normalize_name(candidate.name), []).append(candidate)
    return index


@dataclass
class Pin:
    name: str
    version: str
    path: Path
    sha256: str
    via: List[str]


def resolve(requirements: List[str], index: Dict[str, List[Candidate]], root_name: str) -> List[Pin]:
    pins: Dict[str, Pin] = {}
    constraints: Dict[str, List[Tuple[Requirement, str]]] = {}
    queue: List[Tuple[Requirement, str]] = [(parse_requirement(text), root_name) for text in requirements]
    while queue:
        requirement, parent = queue.pop(0)
        key = requirement.key
        constraints.setdefault(key, []).append((requirement, parent))
        if key in pins:
            pinned = parse_version(pins[key].version)
            if not requirement.allows(pinned):
                others = ", ".join(f"{p} wants {r.text}" for r, p in constraints[key])
                raise ShipitError(f"conflict on {requirement.name}: {pins[key].version} is pinned but {others}; shipit does not backtrack")
            if parent not in pins[key].via:
                pins[key].via.append(parent)
            continue
        candidates = [c for c in index.get(key, []) if all(r.allows(c.version) for r, _ in constraints[key])]
        if not candidates:
            available = ", ".join(str(c.version) for c in index.get(key, [])) or "none"
            raise ShipitError(f"no version of {requirement.name} satisfies {requirement.text} (required by {parent}); available: {available}")
        best = max(candidates, key=lambda c: c.version.key)
        pins[key] = Pin(best.name, str(best.version), best.path, best.sha256, [parent])
        queue.extend((parse_requirement(text), best.name) for text in best.requires)
    return [pins[key] for key in sorted(pins)]


def lock_text(pins: List[Pin], project_name: str, version: str) -> str:
    lines = [
        f"# shipit lock for {project_name} {version}",
        "# install with: pip install --no-index --find-links INDEX --require-hashes -r THIS_FILE",
    ]
    for pin in pins:
        lines.append(f"{normalize_name(pin.name)}=={pin.version} \\")
        lines.append(f"    --hash=sha256:{pin.sha256}")
        lines.append(f"    # via {', '.join(sorted(pin.via))}")
    return "\n".join(lines) + "\n"


@dataclass
class LockEntry:
    name: str
    version: str
    hashes: List[str]


def parse_lock(text: str) -> List[LockEntry]:
    joined = re.sub(r"\\\n", " ", text)
    entries = []
    for line in joined.splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s]+)$", tokens[0])
        if not match:
            raise ShipitError(f"lock entries must be exact pins (name==version), got {tokens[0]!r}")
        hashes = [token.split("=", 1)[1] for token in tokens[1:] if token.startswith("--hash=")]
        if not hashes or not all(h.startswith("sha256:") for h in hashes):
            raise ShipitError(f"{tokens[0]} has no --hash=sha256:... in the lock; every pin needs one")
        entries.append(LockEntry(match.group(1), match.group(2), [h.split(":", 1)[1] for h in hashes]))
    return entries


def locate_locked(entries: List[LockEntry], find_links: Path) -> List[Tuple[LockEntry, Path]]:
    located = []
    for entry in entries:
        path = find_links / wheel_filename(entry.name, entry.version)
        if not path.is_file():
            raise ShipitError(f"{entry.name}=={entry.version}: {path.name} is not in {find_links}")
        located.append((entry, path))
    return located


def verify_hashes(located: List[Tuple[LockEntry, Path]]) -> None:
    bad = []
    for entry, path in located:
        actual = file_sha256(path)
        if actual not in entry.hashes:
            bad.append(f"{entry.name}=={entry.version}: hash mismatch for {path.name}: expected {', '.join(entry.hashes)}, got {actual}")
    if bad:
        raise ShipitError("refusing to install, nothing was written:\n  " + "\n  ".join(bad))


def script_text(target: str) -> str:
    module, _, function = target.partition(":")
    return (
        "#!/usr/bin/env python3\n"
        "import os\nimport sys\n"
        "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        f"from {module} import {function.split('.')[0]}\n"
        f"sys.exit({function}())\n"
    )


@dataclass
class InstallResult:
    name: str
    version: str
    action: str
    files: int


def installed_dist_infos(target: Path, name: str) -> List[Path]:
    if not target.is_dir():
        return []
    return [path for path in target.glob("*.dist-info") if normalize_name(path.name[:-len(".dist-info")].rsplit("-", 1)[0]) == normalize_name(name)]


def remove_installed(target: Path, dist_info: Path) -> None:
    record = dist_info / "RECORD"
    if record.is_file():
        for row in csv.reader(io.StringIO(record.read_text(encoding="utf-8"))):
            if row and not unsafe_path(row[0]):
                (target / row[0]).unlink(missing_ok=True)
    entry_points = dist_info / "entry_points.txt"
    if entry_points.is_file():
        for script in parse_entry_points(entry_points.read_text(encoding="utf-8")):
            (target / "bin" / script).unlink(missing_ok=True)
    shutil.rmtree(dist_info, ignore_errors=True)


def install_wheel(path: Path, target: Path, dry_run: bool = False) -> InstallResult:
    info = read_wheel(path)
    if info.problems:
        raise ShipitError(f"{path.name} failed verification, nothing was written:\n  " + "\n  ".join(info.problems))
    display = info.metadata.get("Name", [info.name])[0]
    with zipfile.ZipFile(path) as archive:
        new_record = archive.read(f"{info.dist_info}/RECORD")
        existing = installed_dist_infos(target, info.name)
        current = target / info.dist_info / "RECORD"
        if current.is_file() and current.read_bytes() == new_record:
            return InstallResult(display, info.version, "unchanged", 0)
        action = "upgraded" if existing else "installed"
        if dry_run:
            return InstallResult(display, info.version, f"would be {action}", len(info.files))
        for old in existing:
            remove_installed(target, old)
        for entry in info.files:
            if entry.endswith("/"):
                continue
            destination = target / entry
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(entry))
    (target / info.dist_info / "INSTALLER").write_text("shipit\n", encoding="utf-8")
    for script, entry_point in sorted(info.scripts.items()):
        script_path = target / "bin" / script
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(script_text(entry_point), encoding="utf-8")
        script_path.chmod(0o755)
    return InstallResult(display, info.version, action, len(info.files))


def install_locked(lock_path: Path, find_links: Path, target: Path, dry_run: bool = False) -> List[InstallResult]:
    located = locate_locked(parse_lock(lock_path.read_text(encoding="utf-8")), find_links)
    verify_hashes(located)
    return [install_wheel(path, target, dry_run) for _, path in located]


def zipapp_main(target: str) -> str:
    module, _, function = target.partition(":")
    return f"import sys\nfrom {module} import {function.split('.')[0]}\nsys.exit({function}())\n"


def build_zipapp(project: Project, version: str, out: Path, epoch: int, dependency_wheels: List[Path], script: Optional[str] = None, interpreter: str = "/usr/bin/env python3", dry_run: bool = False) -> BuildResult:
    if script is None:
        if len(project.scripts) != 1:
            raise ConfigError(f"choose the entry point with --script; project.scripts has {sorted(project.scripts) or 'none'}")
        script = next(iter(project.scripts))
    if script not in project.scripts:
        raise ConfigError(f"no console script named {script!r}; project.scripts has {sorted(project.scripts)}")
    entries: Dict[str, bytes] = dict(wheel_entries(project, version, package_files(project)))
    for wheel in dependency_wheels:
        info = read_wheel(wheel)
        if info.problems:
            raise ShipitError(f"{wheel.name} failed verification: {info.problems[0]}")
        with zipfile.ZipFile(wheel) as archive:
            for entry in info.files:
                if not entry.endswith("/"):
                    entries.setdefault(entry, archive.read(entry))
    entries["__main__.py"] = zipapp_main(project.scripts[script]).encode("utf-8")
    buffer = io.BytesIO()
    buffer.write(f"#!{interpreter}\n".encode("utf-8"))
    write_zip(buffer, sorted(entries.items()), epoch)
    data = buffer.getvalue()
    if not dry_run:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        out.chmod(0o755)
    return BuildResult(out, project.name, version, hashlib.sha256(data).hexdigest(), sorted(entries), epoch)


def build_index(spec_path: Path, out_dir: Path, epoch: int) -> List[BuildResult]:
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read index spec {spec_path}: {error}")
    results = []
    for package in spec["packages"]:
        project = Project(
            name=package["name"],
            version=normalize_version(package["version"]),
            summary=package.get("summary", ""),
            requires_python=package.get("requires-python"),
            dependencies=list(package.get("requires", [])),
            license=package.get("license"),
        )
        files = sorted((path, text.encode("utf-8")) for path, text in package["files"].items())
        project.import_names = sorted({path.split("/")[0].replace(".py", "") for path, _ in files})
        results.append(build_wheel(project, project.version, out_dir, epoch, files))
    return results


class Output:
    def __init__(self, as_json: bool, quiet: bool, stdout=None, stderr=None):
        self.as_json = as_json
        self.quiet = quiet
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr

    def result(self, data, text: str) -> None:
        if self.as_json:
            self.stdout.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        elif not self.quiet and text:
            self.stdout.write(text.rstrip("\n") + "\n")

    def warn(self, message: str) -> None:
        self.stderr.write(f"shipit: warning: {message}\n")


def project_from(args) -> Project:
    return load_project(Path(args.project).resolve())


def project_version(project: Project, output: Output, runner: Runner = run_command) -> str:
    version = resolve_version(project, runner)
    if project.version_source == "git":
        declared = module_version(project)
        if declared and declared != version:
            output.warn(f"{project.module}.__version__ is {declared} but the git tag gives {version}; tools that read __version__ will disagree")
    for key in project.ignored:
        output.warn(f"{key} is not supported by shipit and was left out of METADATA")
    return version


def command_version(args, output: Output) -> int:
    project = project_from(args)
    version = project_version(project, output)
    data = {"name": project.name, "version": version, "source": project.version_source}
    if project.version_source == "git":
        git = version_from_git(project.root)
        data.update({"tag": git.tag, "distance": git.distance, "commit": git.commit, "dirty": git.dirty})
    output.result(data, version)
    return EXIT_OK


def command_build(args, output: Output) -> int:
    project = project_from(args)
    version = project_version(project, output)
    epoch = source_date_epoch(dict(os.environ), project.root)
    result = build_wheel(project, version, Path(args.out), epoch, dry_run=args.dry_run)
    data = {"wheel": str(result.path), "name": result.name, "version": result.version, "sha256": result.sha256, "files": result.files, "source_date_epoch": epoch, "dry_run": args.dry_run}
    prefix = "would build" if args.dry_run else "built"
    output.result(data, f"{prefix} {result.path} sha256={result.sha256}")
    return EXIT_OK


def command_inspect(args, output: Output) -> int:
    info = read_wheel(Path(args.wheel))
    data = {"name": info.name, "version": info.version, "tag": info.tag, "files": info.files, "requires": info.requires, "scripts": info.scripts, "problems": info.problems, "metadata": {k: v for k, v in info.metadata.items() if k != "Description"}}
    lines = [f"{info.name} {info.version} {info.tag}"] + [f"  {name}" for name in info.files]
    lines += [f"  requires {req}" for req in info.requires] + [f"  script {k} = {v}" for k, v in info.scripts.items()]
    lines += [f"PROBLEM {problem}" for problem in info.problems] or ["RECORD verified"]
    output.result(data, "\n".join(lines))
    return EXIT_FAIL if info.problems else EXIT_OK


def command_index(args, output: Output) -> int:
    epoch = source_date_epoch(dict(os.environ), None)
    results = build_index(Path(args.spec), Path(args.out), epoch)
    output.result([{"wheel": str(r.path), "sha256": r.sha256} for r in results], "\n".join(f"built {r.path.name}" for r in results))
    return EXIT_OK


def command_lock(args, output: Output) -> int:
    project = project_from(args)
    version = project_version(project, output)
    pins = resolve(project.dependencies, scan_index(Path(args.find_links)), project.name)
    text = lock_text(pins, project.name, version)
    if args.out and not args.dry_run:
        Path(args.out).write_text(text, encoding="utf-8")
    data = [{"name": p.name, "version": p.version, "sha256": p.sha256, "via": p.via, "wheel": p.path.name} for p in pins]
    output.result(data, text if not args.out else "\n".join(f"{'would pin' if args.dry_run else 'pinned'} {p.name}=={p.version}" for p in pins))
    return EXIT_OK


def command_install(args, output: Output) -> int:
    target = Path(args.target)
    if not args.lock and not args.wheels:
        raise ConfigError("nothing to install: pass wheel files or --lock FILE --find-links DIR")
    if args.lock and not args.find_links:
        raise ConfigError("--lock needs --find-links DIR to locate the wheels")
    results = install_locked(Path(args.lock), Path(args.find_links), target, args.dry_run) if args.lock else []
    results += [install_wheel(Path(wheel), target, args.dry_run) for wheel in args.wheels]
    output.result([r.__dict__ for r in results], "\n".join(f"{r.action} {r.name} {r.version}" for r in results))
    return EXIT_OK


def command_zipapp(args, output: Output) -> int:
    project = project_from(args)
    version = project_version(project, output)
    wheels: List[Path] = []
    if args.lock:
        if not args.find_links:
            raise ConfigError("--lock needs --find-links DIR to locate the wheels")
        located = locate_locked(parse_lock(Path(args.lock).read_text(encoding="utf-8")), Path(args.find_links))
        verify_hashes(located)
        wheels = [path for _, path in located]
    elif project.dependencies:
        output.warn(f"{project.name} has dependencies {project.dependencies} but no --lock was given; the zipapp will not contain them")
    epoch = source_date_epoch(dict(os.environ), project.root)
    out = Path(args.out or f"{normalize_name(project.name)}.pyz")
    result = build_zipapp(project, version, out, epoch, wheels, args.script, args.python, args.dry_run)
    output.result({"zipapp": str(result.path), "sha256": result.sha256, "files": result.files, "dry_run": args.dry_run}, f"{'would build' if args.dry_run else 'built'} {result.path} sha256={result.sha256}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    common.add_argument("--quiet", action="store_true", help="print nothing on success")
    parser = argparse.ArgumentParser(prog="shipit", description="Build, lock and install Python tools from scratch.", epilog="exit codes: 0 ok, 1 build or verification failure, 2 usage or configuration error")
    sub = parser.add_subparsers(dest="command", required=True)
    version = sub.add_parser("version", parents=[common], help="print the version from pyproject or the latest git tag")
    version.add_argument("--project", default=".")
    build = sub.add_parser("build", parents=[common], help="build a reproducible wheel")
    build.add_argument("--project", default=".")
    build.add_argument("--out", default="dist")
    build.add_argument("--dry-run", action="store_true")
    inspect = sub.add_parser("inspect", parents=[common], help="verify a wheel's RECORD and print its metadata")
    inspect.add_argument("wheel")
    index = sub.add_parser("index", parents=[common], help="build the fixture index of dependency wheels")
    index.add_argument("spec")
    index.add_argument("--out", required=True)
    lock = sub.add_parser("lock", parents=[common], help="pin dependencies with hashes from a find-links directory")
    lock.add_argument("--project", default=".")
    lock.add_argument("--find-links", required=True)
    lock.add_argument("--out")
    lock.add_argument("--dry-run", action="store_true")
    install = sub.add_parser("install", parents=[common], help="verify hashes, then install wheels into a target directory")
    install.add_argument("wheels", nargs="*")
    install.add_argument("--lock")
    install.add_argument("--find-links")
    install.add_argument("--target", required=True)
    install.add_argument("--dry-run", action="store_true")
    zipapp_parser = sub.add_parser("zipapp", parents=[common], help="build a single-file executable zip with the locked dependencies")
    zipapp_parser.add_argument("--project", default=".")
    zipapp_parser.add_argument("--out")
    zipapp_parser.add_argument("--lock")
    zipapp_parser.add_argument("--find-links")
    zipapp_parser.add_argument("--script")
    zipapp_parser.add_argument("--python", default="/usr/bin/env python3")
    zipapp_parser.add_argument("--dry-run", action="store_true")
    return parser


COMMANDS = {
    "version": command_version,
    "build": command_build,
    "inspect": command_inspect,
    "index": command_index,
    "lock": command_lock,
    "install": command_install,
    "zipapp": command_zipapp,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exit_:
        return EXIT_USAGE if exit_.code else EXIT_OK
    output = Output(args.json, args.quiet)
    try:
        return COMMANDS[args.command](args, output)
    except ShipitError as error:
        sys.stderr.write(f"shipit: {error}\n")
        return error.code
    except OSError as error:
        sys.stderr.write(f"shipit: {error}\n")
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
