"""minigit: Git's object store from scratch: loose blobs, trees, commits and tags, refs and HEAD, a reflog, log, checkout and fsck."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

OBJECT_TYPES = ("blob", "tree", "commit", "tag")
HASH_BYTES = {"sha1": 20, "sha256": 32}
MODE_FILE = "100644"
MODE_EXEC = "100755"
MODE_LINK = "120000"
MODE_TREE = "40000"
MODE_GITLINK = "160000"
MODE_TYPES = {MODE_FILE: "blob", MODE_EXEC: "blob", MODE_LINK: "blob", MODE_TREE: "tree", MODE_GITLINK: "commit"}
LOOSE_COMPRESSION = 1
SYMREF_MAXDEPTH = 5
DWIM_RULES = ("{}", "refs/{}", "refs/tags/{}", "refs/heads/{}", "refs/remotes/{}", "refs/remotes/{}/HEAD")
LOGGED_PREFIXES = ("refs/heads/", "refs/remotes/", "refs/notes/")
IDENT = re.compile(r"(?P<name>[^<>\n]*?) ?<(?P<email>[^<>\n]*)> (?P<time>[0-9]+) (?P<tz>[+-][0-9]{4})$")
RAW_DATE = re.compile(r"@?(?P<time>[0-9]+)(?: (?P<tz>[+-][0-9]{4}))?$")
ISO_DATE = re.compile(r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})[T ](?P<clock>[0-9]{2}:[0-9]{2}(?::[0-9]{2})?)\s*(?P<tz>Z|[+-][0-9]{2}:?[0-9]{2})?$")
SUFFIX = re.compile(r"\^\{(?P<peel>[a-z]*)\}|\^(?P<parent>[0-9]*)|~(?P<ancestor>[0-9]*)")
REFLOG_SELECTOR = re.compile(r"(?P<name>.*)@\{(?P<index>[0-9]+)\}$")
HEX = re.compile(r"[0-9a-f]+$")
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
WARNING_CHECKS = {"zeroPaddedFilemode", "badFilemode"}


class GitError(Exception):
    pass


class ObjectMissing(GitError):
    pass


@dataclass
class Repo:
    git_dir: str
    work_tree: Optional[str]
    algo: str = "sha1"
    config: Dict[str, str] = field(default_factory=dict)

    @property
    def hex_len(self) -> int:
        return HASH_BYTES[self.algo] * 2

    @property
    def zero(self) -> str:
        return "0" * self.hex_len


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    name: str
    oid: str

    @property
    def kind(self) -> str:
        return MODE_TYPES.get(self.mode, "blob")


@dataclass(frozen=True)
class Ident:
    name: str
    email: str
    time: int
    tz: str

    def encode(self) -> str:
        return f"{self.name} <{self.email}> {self.time} {self.tz}"


@dataclass
class Commit:
    tree: str
    parents: List[str]
    author: Ident
    committer: Ident
    message: str
    extra: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class Tag:
    object: str
    type: str
    tag: str
    tagger: Optional[Ident]
    message: str


@dataclass(frozen=True)
class ReflogEntry:
    old: str
    new: str
    ident: Ident
    message: str


@dataclass(frozen=True)
class Problem:
    oid: str
    kind: str
    check: str
    detail: str

    @property
    def severity(self) -> str:
        return "warning" if self.check in WARNING_CHECKS else "error"


@dataclass
class FsckReport:
    loose: int
    reachable: int
    roots: int
    packs: int
    problems: List[Problem]
    dangling: List[Tuple[str, str]]

    def errors(self, strict: bool) -> List[Problem]:
        return [p for p in self.problems if strict or p.severity == "error"]


def frame(kind: str, body: bytes) -> bytes:
    return kind.encode() + b" " + str(len(body)).encode() + b"\0" + body


def hash_object(kind: str, body: bytes, algo: str = "sha1") -> str:
    return hashlib.new(algo, frame(kind, body)).hexdigest()


def unframe(raw: bytes) -> Tuple[str, bytes]:
    header, nul, body = raw.partition(b"\0")
    if not nul:
        raise GitError("object header has no NUL terminator")
    kind, space, size = header.partition(b" ")
    if not space or kind.decode("ascii", "replace") not in OBJECT_TYPES:
        raise GitError(f"object header {header[:32]!r} does not start with blob, tree, commit or tag")
    if not size.isdigit() or (size.startswith(b"0") and size != b"0"):
        raise GitError(f"object header has a bad size {size!r}")
    if int(size) != len(body):
        raise GitError(f"object header says {int(size)} bytes, the body has {len(body)}")
    return kind.decode(), body


def name_bytes(name: str) -> bytes:
    return name.encode("utf-8", "surrogateescape")


def name_text(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def object_path(repo: Repo, oid: str) -> str:
    return os.path.join(repo.git_dir, "objects", oid[:2], oid[2:])


def pack_files(repo: Repo) -> List[str]:
    pack_dir = os.path.join(repo.git_dir, "objects", "pack")
    if not os.path.isdir(pack_dir):
        return []
    return sorted(os.path.join(pack_dir, n) for n in os.listdir(pack_dir) if n.endswith(".pack"))


def write_object(repo: Repo, kind: str, body: bytes) -> str:
    raw = frame(kind, body)
    oid = hashlib.new(repo.algo, raw).hexdigest()
    path = object_path(repo, oid)
    if os.path.exists(path):
        return oid
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="tmp_obj_", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(zlib.compress(raw, LOOSE_COMPRESSION))
        os.chmod(tmp, 0o444)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return oid


def read_raw(repo: Repo, oid: str) -> bytes:
    path = object_path(repo, oid)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        hint = ""
        if pack_files(repo):
            hint = f" as a loose object; this repository has {len(pack_files(repo))} packfile(s) and minigit reads loose objects only"
        raise ObjectMissing(f"object {oid} not found{hint}") from None
    if not data:
        raise GitError(f"object file {path} is empty")
    try:
        return zlib.decompress(data)
    except zlib.error:
        raise GitError(f"loose object {oid} (stored in {path}) is corrupt") from None


def read_object(repo: Repo, oid: str) -> Tuple[str, bytes]:
    return unframe(read_raw(repo, oid))


def has_object(repo: Repo, oid: str) -> bool:
    return os.path.isfile(object_path(repo, oid))


def loose_objects(repo: Repo) -> List[str]:
    root = os.path.join(repo.git_dir, "objects")
    found = []
    for fan in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if len(fan) != 2 or not HEX.match(fan):
            continue
        for rest in sorted(os.listdir(os.path.join(root, fan))):
            if len(rest) == repo.hex_len - 2 and HEX.match(rest):
                found.append(fan + rest)
    return found


def expand_prefix(repo: Repo, prefix: str) -> Optional[str]:
    if len(prefix) < 4 or len(prefix) > repo.hex_len or not HEX.match(prefix):
        return None
    fan = os.path.join(repo.git_dir, "objects", prefix[:2])
    if not os.path.isdir(fan):
        return None
    matches = [prefix[:2] + n for n in os.listdir(fan) if n.startswith(prefix[2:]) and len(n) == repo.hex_len - 2]
    if len(matches) > 1:
        raise GitError(f"short object ID {prefix} is ambiguous: {', '.join(sorted(matches))}")
    return matches[0] if matches else None


def sort_key(entry: TreeEntry) -> bytes:
    return name_bytes(entry.name) + (b"/" if entry.mode == MODE_TREE else b"")


def encode_tree(entries: Iterable[TreeEntry], keep_order: bool = False) -> bytes:
    ordered = list(entries) if keep_order else sorted(entries, key=sort_key)
    return b"".join(e.mode.encode() + b" " + name_bytes(e.name) + b"\0" + bytes.fromhex(e.oid) for e in ordered)


def parse_tree(body: bytes, algo: str = "sha1") -> List[TreeEntry]:
    size = HASH_BYTES[algo]
    entries = []
    pos = 0
    while pos < len(body):
        space = body.find(b" ", pos)
        nul = body.find(b"\0", space + 1)
        if space < 0 or nul < 0 or nul + 1 + size > len(body):
            raise GitError(f"truncated tree entry at byte {pos}")
        mode = body[pos:space].decode("ascii", "replace")
        entries.append(TreeEntry(mode, name_text(body[space + 1:nul]), body[nul + 1:nul + 1 + size].hex()))
        pos = nul + 1 + size
    return entries


def ls_tree_line(entry: TreeEntry, path: str = "") -> str:
    mode = f"{int(entry.mode, 8):06o}" if entry.mode.isdigit() else entry.mode
    return f"{mode} {entry.kind} {entry.oid}\t{path or entry.name}"


def tree_problems(entries: Sequence[TreeEntry]) -> List[Tuple[str, str]]:
    found = []
    seen: Set[str] = set()
    for i, entry in enumerate(entries):
        lowered = entry.name.lower()
        if entry.name == "":
            found.append(("emptyName", "an entry has an empty name"))
        elif "/" in entry.name:
            found.append(("fullPathname", f"{entry.name!r} contains a slash"))
        elif entry.name == ".":
            found.append(("hasDot", "an entry is named '.'"))
        elif entry.name == "..":
            found.append(("hasDotdot", "an entry is named '..'"))
        elif lowered == ".git":
            found.append(("hasDotgit", f"an entry is named {entry.name!r}"))
        if entry.mode.startswith("0"):
            found.append(("zeroPaddedFilemode", f"{entry.name}: mode {entry.mode} has a leading zero"))
        elif entry.mode not in MODE_TYPES:
            found.append(("badFilemode", f"{entry.name}: mode {entry.mode} is not one of 100644, 100755, 120000, 40000, 160000"))
        if entry.name in seen:
            found.append(("duplicateEntries", f"{entry.name!r} appears twice"))
        seen.add(entry.name)
        if i and sort_key(entries[i - 1]) >= sort_key(entry) and entries[i - 1].name != entry.name:
            found.append(("treeNotSorted", f"{entries[i - 1].name!r} is listed before {entry.name!r}"))
    return found


def parse_ident(text: str) -> Ident:
    match = IDENT.match(text)
    if not match:
        raise GitError(f"not a Git identity line: {text!r} (expected: Name <email> 1700000000 +0100)")
    return Ident(match["name"], match["email"], int(match["time"]), match["tz"])


def ident_problem(text: str) -> Optional[Tuple[str, str]]:
    if "<" not in text:
        return ("missingEmail", f"{text!r} has no <email>")
    if not re.search(r"> ", text):
        return ("missingSpaceBeforeDate", f"{text!r} has no date after the email")
    tail = text.rsplit("> ", 1)[1]
    parts = tail.split(" ")
    if len(parts) != 2 or not parts[0].isdigit():
        return ("badDate", f"{tail!r} is not a Unix time and a zone")
    if parts[0].startswith("0") and parts[0] != "0":
        return ("zeroPaddedDate", f"{parts[0]} has a leading zero")
    if not re.match(r"[+-][0-9]{4}$", parts[1]):
        return ("badTimezone", f"{parts[1]!r} is not +HHMM or -HHMM")
    return None


def tz_string(offset_seconds: int) -> str:
    sign = "-" if offset_seconds < 0 else "+"
    minutes = abs(offset_seconds) // 60
    return f"{sign}{minutes // 60:02d}{minutes % 60:02d}"


def tz_seconds(tz: str) -> int:
    sign = -1 if tz[0] == "-" else 1
    return sign * (int(tz[1:3]) * 3600 + int(tz[3:5]) * 60)


def local_tz(when: float) -> str:
    return tz_string(time.localtime(when).tm_gmtoff)


def parse_date(text: str) -> Tuple[int, str]:
    raw = text.strip()
    match = RAW_DATE.match(raw)
    if match:
        when = int(match["time"])
        return when, match["tz"] or "+0000"
    match = ISO_DATE.match(raw)
    if not match:
        raise GitError(f"cannot parse date {text!r} (examples: '@1767600000 +0100', '2026-01-05T09:00:00+01:00')")
    clock = match["clock"] if match["clock"].count(":") == 2 else match["clock"] + ":00"
    zone = match["tz"]
    naive = dt.datetime.fromisoformat(f"{match['date']}T{clock}")
    if zone is None:
        when = int(time.mktime(naive.timetuple()))
        return when, local_tz(when)
    tz = "+0000" if zone == "Z" else zone.replace(":", "")
    when = int((naive - dt.datetime(1970, 1, 1)).total_seconds()) - tz_seconds(tz)
    return when, tz


def format_date(when: int, tz: str) -> str:
    moment = dt.datetime.fromtimestamp(when, dt.timezone(dt.timedelta(seconds=tz_seconds(tz))))
    return f"{DAYS[moment.weekday()]} {MONTHS[moment.month - 1]} {moment.day} {moment:%H:%M:%S} {moment.year} {tz}"


def iso_date(when: int, tz: str) -> str:
    moment = dt.datetime.fromtimestamp(when, dt.timezone(dt.timedelta(seconds=tz_seconds(tz))))
    return moment.isoformat()


def clean_name(text: str) -> str:
    return re.sub(r"[<>\n]", "", text).strip()


def make_ident(repo: Repo, role: str, env: Mapping[str, str], now: float) -> Ident:
    name = clean_name(env.get(f"GIT_{role}_NAME") or repo.config.get("user.name", ""))
    email = clean_name(env.get(f"GIT_{role}_EMAIL") or repo.config.get("user.email", "") or env.get("EMAIL", ""))
    if not name or not email:
        raise GitError(f"{role.lower()} identity unknown: set GIT_{role}_NAME and GIT_{role}_EMAIL, or user.name and user.email in the config")
    date = env.get(f"GIT_{role}_DATE")
    when, tz = parse_date(date) if date else (int(now), local_tz(now))
    return Ident(name, email, when, tz)


def parse_headers(body: bytes) -> Tuple[List[Tuple[str, str]], str]:
    text = body.decode("utf-8", "surrogateescape")
    head, _, message = text.partition("\n\n")
    headers: List[Tuple[str, str]] = []
    for line in head.split("\n") if head else []:
        if line.startswith(" ") and headers:
            key, value = headers[-1]
            headers[-1] = (key, value + "\n" + line[1:])
        else:
            key, _, value = line.partition(" ")
            headers.append((key, value))
    return headers, message


def encode_commit(commit: Commit) -> bytes:
    lines = [f"tree {commit.tree}"] + [f"parent {p}" for p in commit.parents]
    lines += [f"author {commit.author.encode()}", f"committer {commit.committer.encode()}"]
    lines += [f"{key} {value.replace(chr(10), chr(10) + ' ')}" for key, value in commit.extra]
    return ("\n".join(lines) + "\n\n" + commit.message).encode("utf-8", "surrogateescape")


def parse_commit(body: bytes) -> Commit:
    headers, message = parse_headers(body)
    values: Dict[str, List[str]] = {}
    for key, value in headers:
        values.setdefault(key, []).append(value)
    if "tree" not in values or "author" not in values or "committer" not in values:
        raise GitError("commit is missing its tree, author or committer header")
    extra = [(k, v) for k, v in headers if k not in ("tree", "parent", "author", "committer")]
    return Commit(values["tree"][0], values.get("parent", []), parse_ident(values["author"][0]), parse_ident(values["committer"][0]), message, extra)


def encode_tag(tag: Tag) -> bytes:
    lines = [f"object {tag.object}", f"type {tag.type}", f"tag {tag.tag}"]
    if tag.tagger:
        lines.append(f"tagger {tag.tagger.encode()}")
    return ("\n".join(lines) + "\n\n" + tag.message).encode("utf-8", "surrogateescape")


def parse_tag(body: bytes) -> Tag:
    headers, message = parse_headers(body)
    values = dict(headers)
    if not {"object", "type", "tag"} <= set(values):
        raise GitError("tag is missing its object, type or tag header")
    tagger = parse_ident(values["tagger"]) if "tagger" in values else None
    return Tag(values["object"], values["type"], values["tag"], tagger, message)


def complete_line(message: str) -> str:
    return message if not message or message.endswith("\n") else message + "\n"


def cleanup_message(message: str) -> str:
    lines = [line.rstrip() for line in message.split("\n")]
    kept: List[str] = []
    for line in lines:
        if line or (kept and kept[-1]):
            kept.append(line)
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(kept) + "\n" if kept else ""


def read_config(path: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    section = ""
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line[0] in "#;":
                continue
            header = re.match(r'\[([A-Za-z0-9.-]+)(?:\s+"(.*)")?\]$', line)
            if header:
                section = header.group(1).lower() + (f".{header.group(2)}" if header.group(2) else "")
                continue
            key, equals, value = line.partition("=")
            values[f"{section}.{key.strip().lower()}"] = value.strip().strip('"') if equals else "true"
    return values


def is_git_dir(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "HEAD")) and os.path.isdir(os.path.join(path, "objects"))


def open_git_dir(git_dir: str, work_tree: Optional[str]) -> Repo:
    if os.path.isfile(git_dir):
        with open(git_dir, encoding="utf-8") as handle:
            pointer = handle.read().strip()
        if not pointer.startswith("gitdir: "):
            raise GitError(f"{git_dir} is a file but not a 'gitdir: PATH' pointer")
        git_dir = os.path.join(os.path.dirname(git_dir), pointer[len("gitdir: "):])
    if not is_git_dir(git_dir):
        raise GitError(f"{git_dir} is not a Git directory (no HEAD or objects/)")
    config = read_config(os.path.join(git_dir, "config"))
    for key, value in read_config(os.environ.get("GIT_CONFIG_GLOBAL") or os.path.expanduser("~/.gitconfig")).items():
        if key.startswith("user."):
            config.setdefault(key, value)
    algo = config.get("extensions.objectformat", "sha1").lower()
    if algo not in HASH_BYTES:
        raise GitError(f"unknown object format {algo!r} in {git_dir}/config")
    if config.get("extensions.refstorage", "files").lower() != "files":
        raise GitError("this repository stores refs in reftable; minigit reads the files backend only (git init --ref-format=files)")
    return Repo(os.path.abspath(git_dir), os.path.abspath(work_tree) if work_tree else None, algo, config)


def find_repo(start: str = ".") -> Repo:
    path = os.path.abspath(start)
    while True:
        dot = os.path.join(path, ".git")
        if os.path.exists(dot):
            return open_git_dir(dot, path)
        if is_git_dir(path):
            return open_git_dir(path, None)
        parent = os.path.dirname(path)
        if parent == path:
            raise GitError(f"not a git repository (or any of the parent directories): {start}")
        path = parent


def init_repo(path: str, algo: str = "sha1", branch: str = "main", bare: bool = False) -> Tuple[Repo, bool]:
    if algo not in HASH_BYTES:
        raise GitError(f"unknown object format {algo!r} (sha1 or sha256)")
    if check_ref_format(f"refs/heads/{branch}"):
        raise GitError(f"invalid initial branch name {branch!r}")
    git_dir = path if bare else os.path.join(path, ".git")
    existed = is_git_dir(git_dir)
    for sub in ("objects/info", "objects/pack", "refs/heads", "refs/tags"):
        os.makedirs(os.path.join(git_dir, sub), exist_ok=True)
    if not existed:
        with open(os.path.join(git_dir, "HEAD"), "w", encoding="utf-8") as handle:
            handle.write(f"ref: refs/heads/{branch}\n")
        core = ["[core]", f"\trepositoryformatversion = {1 if algo != 'sha1' else 0}", "\tfilemode = true", f"\tbare = {'true' if bare else 'false'}"]
        if not bare:
            core.append("\tlogallrefupdates = true")
        if algo != "sha1":
            core += ["[extensions]", f"\tobjectformat = {algo}"]
        with open(os.path.join(git_dir, "config"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(core) + "\n")
    return open_git_dir(git_dir, None if bare else path), existed


def check_ref_format(name: str) -> Optional[str]:
    if name == "HEAD":
        return None
    if not name.startswith("refs/"):
        return f"{name!r} is not under refs/"
    if name.endswith("/") or "//" in name:
        return f"{name!r} has an empty component"
    if name.endswith("."):
        return f"{name!r} ends with a dot"
    if ".." in name or "@{" in name or name == "@":
        return f"{name!r} contains '..' or '@{{'"
    if re.search(r"[\x00-\x20\x7f~^:?*\[\\]", name):
        return f"{name!r} contains a space, a control character or one of ~ ^ : ? * [ \\"
    for part in name.split("/"):
        if part.startswith(".") or part.endswith(".lock"):
            return f"{name!r} has a component that starts with a dot or ends with .lock"
    return None


def ref_path(repo: Repo, name: str) -> str:
    return os.path.join(repo.git_dir, *name.split("/"))


def packed_refs(repo: Repo) -> Dict[str, str]:
    path = os.path.join(repo.git_dir, "packed-refs")
    refs: Dict[str, str] = {}
    if not os.path.isfile(path):
        return refs
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(("#", "^")) or " " not in line:
                continue
            oid, name = line.rstrip("\n").split(" ", 1)
            refs[name] = oid
    return refs


def read_ref(repo: Repo, name: str) -> Optional[str]:
    path = ref_path(repo, name)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    return packed_refs(repo).get(name)


def follow_symrefs(repo: Repo, name: str) -> Tuple[str, Optional[str]]:
    current = name
    for _ in range(SYMREF_MAXDEPTH + 1):
        value = read_ref(repo, current)
        if value is None:
            return current, None
        if not value.startswith("ref: "):
            if len(value) != repo.hex_len or not HEX.match(value):
                raise GitError(f"ref {current} holds {value!r}, which is neither an object ID nor 'ref: NAME'")
            return current, value
        current = value[len("ref: "):]
    raise GitError(f"symbolic ref {name} is a loop or a chain deeper than {SYMREF_MAXDEPTH}")


def resolve_ref(repo: Repo, name: str) -> Optional[str]:
    return follow_symrefs(repo, name)[1]


def head_target(repo: Repo) -> Optional[str]:
    value = read_ref(repo, "HEAD")
    return value[len("ref: "):] if value and value.startswith("ref: ") else None


def list_refs(repo: Repo) -> Dict[str, str]:
    names: Set[str] = set(packed_refs(repo))
    root = os.path.join(repo.git_dir, "refs")
    for folder, _, files in os.walk(root):
        for name in files:
            if not name.endswith(".lock"):
                names.add(os.path.relpath(os.path.join(folder, name), repo.git_dir).replace(os.sep, "/"))
    resolved = {}
    for name in sorted(names):
        oid = resolve_ref(repo, name)
        if oid:
            resolved[name] = oid
    return resolved


def write_locked(path: str, data: bytes, verify: Callable[[], None]) -> None:
    lock = path + ".lock"
    try:
        os.makedirs(os.path.dirname(lock), exist_ok=True)
    except (FileExistsError, NotADirectoryError):
        raise GitError(f"cannot create {path}: a ref with the name of one of its parent directories exists") from None
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise GitError(f"Unable to create '{lock}': File exists. Another git process seems to be running in this repository, or one crashed and left the lock; remove it once no git process is running.") from None
    except IsADirectoryError:
        raise GitError(f"cannot create {path}: a directory of that name exists") from None
    handle = os.fdopen(fd, "wb")
    try:
        verify()
        handle.write(data)
        handle.close()
        if os.path.isdir(path):
            raise GitError(f"cannot create {path}: a directory of that name exists")
        os.replace(lock, path)
    except BaseException:
        handle.close()
        if os.path.exists(lock):
            os.unlink(lock)
        raise


def should_log(repo: Repo, name: str) -> bool:
    return name == "HEAD" or name.startswith(LOGGED_PREFIXES) or os.path.isfile(os.path.join(repo.git_dir, "logs", *name.split("/")))


def append_reflog(repo: Repo, name: str, old: Optional[str], new: Optional[str], ident: Ident, message: str) -> None:
    path = os.path.join(repo.git_dir, "logs", *name.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = " ".join(message.split())
    line = f"{old or repo.zero} {new or repo.zero} {ident.encode()}" + (f"\t{text}" if text else "") + "\n"
    with open(path, "a", encoding="utf-8", errors="surrogateescape") as handle:
        handle.write(line)


def read_reflog(repo: Repo, name: str) -> List[ReflogEntry]:
    path = os.path.join(repo.git_dir, "logs", *name.split("/"))
    entries = []
    if not os.path.isfile(path):
        return entries
    with open(path, encoding="utf-8", errors="surrogateescape") as handle:
        for line in handle:
            head, _, message = line.rstrip("\n").partition("\t")
            parts = head.split(" ", 2)
            if len(parts) == 3:
                entries.append(ReflogEntry(parts[0], parts[1], parse_ident(parts[2]), message))
    return entries


def update_ref(repo: Repo, name: str, new: str, ident: Ident, old: Optional[str] = None, message: str = "", deref: bool = True) -> str:
    target = follow_symrefs(repo, name)[0] if deref else name
    problem = check_ref_format(target)
    if problem:
        raise GitError(f"refusing to update ref with bad name: {problem}")
    before: List[Optional[str]] = [None]

    def verify() -> None:
        current = resolve_ref(repo, target)
        before[0] = current
        if old is None:
            return
        if old == repo.zero and current is not None:
            raise GitError(f"cannot lock ref '{target}': reference already exists")
        if old != repo.zero and current != old:
            raise GitError(f"cannot lock ref '{target}': is at {current or repo.zero} but expected {old}")

    write_locked(ref_path(repo, target), (new + "\n").encode(), verify)
    if should_log(repo, target):
        append_reflog(repo, target, before[0], new, ident, message)
    if target != "HEAD" and head_target(repo) == target:
        append_reflog(repo, "HEAD", before[0], new, ident, message)
    return target


def delete_ref(repo: Repo, name: str, old: Optional[str] = None) -> str:
    target = follow_symrefs(repo, name)[0]
    current = resolve_ref(repo, target)
    if current is None:
        raise GitError(f"cannot delete ref '{target}': it does not exist")
    if old is not None and old != current:
        raise GitError(f"cannot lock ref '{target}': is at {current} but expected {old}")
    path = ref_path(repo, target)
    if os.path.isfile(path):
        os.unlink(path)
    packed = os.path.join(repo.git_dir, "packed-refs")
    if target in packed_refs(repo):
        with open(packed, encoding="utf-8") as handle:
            lines = handle.readlines()
        kept: List[str] = []
        skip_peeled = False
        for line in lines:
            if line.startswith("^") and skip_peeled:
                continue
            skip_peeled = False
            if not line.startswith(("#", "^")) and line.rstrip("\n").split(" ", 1)[-1] == target:
                skip_peeled = True
                continue
            kept.append(line)
        write_locked(packed, "".join(kept).encode(), lambda: None)
    log = os.path.join(repo.git_dir, "logs", *target.split("/"))
    if os.path.isfile(log):
        os.unlink(log)
    return target


def symbolic_ref(repo: Repo, name: str, target: str, ident: Ident, message: str = "") -> None:
    if not target.startswith("refs/") or check_ref_format(target):
        raise GitError(f"refusing to point {name} outside refs/ or at a bad name: {target!r}")
    old = resolve_ref(repo, name)
    write_locked(ref_path(repo, name), f"ref: {target}\n".encode(), lambda: None)
    new = resolve_ref(repo, target)
    if message and old and new and should_log(repo, name):
        append_reflog(repo, name, old, new, ident, message)


def dwim_refs(repo: Repo, name: str) -> List[str]:
    return [rule.format(name) for rule in DWIM_RULES if read_ref(repo, rule.format(name)) is not None]


def resolve_name(repo: Repo, name: str) -> str:
    if name in ("", "@"):
        name = "HEAD"
    selector = REFLOG_SELECTOR.match(name)
    if selector:
        return reflog_value(repo, selector["name"] or "HEAD", int(selector["index"]))
    if len(name) == repo.hex_len and HEX.match(name):
        return name
    for ref in dwim_refs(repo, name):
        final, oid = follow_symrefs(repo, ref)
        if oid:
            return oid
        raise GitError(f"{name} points to {final}, which has no commit yet (an unborn branch)")
    oid = expand_prefix(repo, name)
    if oid:
        return oid
    raise GitError(f"unknown revision {name!r}: not a ref, a full object ID or a unique prefix of a loose object")


def reflog_value(repo: Repo, name: str, index: int) -> str:
    refs = dwim_refs(repo, name) if name != "HEAD" else ["HEAD"]
    if not refs:
        raise GitError(f"unknown revision {name!r}")
    ref = follow_symrefs(repo, refs[0])[0] if name != "HEAD" else "HEAD"
    entries = read_reflog(repo, ref)
    if index >= len(entries):
        raise GitError(f"log for '{name}' only has {len(entries)} entries")
    return entries[len(entries) - 1 - index].new


def peel(repo: Repo, oid: str, want: str) -> str:
    current = oid
    while True:
        kind, body = read_object(repo, current)
        if kind == want or (want == "" and kind != "tag"):
            return current
        if kind == "tag":
            current = parse_tag(body).object
        elif kind == "commit" and want == "tree":
            current = parse_commit(body).tree
        else:
            raise GitError(f"{oid} is a {kind}, it does not lead to a {want}")


def lookup_path(repo: Repo, tree: str, path: str) -> str:
    current = tree
    for part in [p for p in path.split("/") if p]:
        kind, body = read_object(repo, current)
        if kind != "tree":
            raise GitError(f"path '{path}' does not exist: {part!r} is inside a {kind}")
        match = [e for e in parse_tree(body, repo.algo) if e.name == part]
        if not match:
            raise GitError(f"path '{path}' does not exist in tree {tree}")
        current = match[0].oid
    return current


def rev_parse(repo: Repo, spec: str) -> str:
    if ":" in spec:
        rev, path = spec.split(":", 1)
        if not rev:
            raise GitError(f"{spec!r} names the index, which minigit does not have; use HEAD:{path}")
        return lookup_path(repo, peel(repo, rev_parse(repo, rev), "tree"), path)
    cut = len(spec)
    for i, char in enumerate(spec):
        if char in "^~":
            cut = i
            break
    oid = resolve_name(repo, spec[:cut])
    pos = cut
    while pos < len(spec):
        match = SUFFIX.match(spec, pos)
        if not match:
            raise GitError(f"cannot parse revision suffix {spec[pos:]!r} in {spec!r}")
        if match["peel"] is not None:
            oid = peel(repo, oid, match["peel"])
        elif match["ancestor"] is not None:
            for _ in range(int(match["ancestor"] or 1)):
                oid = nth_parent(repo, oid, 1, spec)
        else:
            number = int(match["parent"] or 1)
            oid = peel(repo, oid, "commit") if number == 0 else nth_parent(repo, oid, number, spec)
        pos = match.end()
    return oid


def nth_parent(repo: Repo, oid: str, number: int, spec: str) -> str:
    commit = parse_commit(read_object(repo, peel(repo, oid, "commit"))[1])
    if number > len(commit.parents):
        raise GitError(f"{spec}: commit {oid[:7]} has {len(commit.parents)} parent(s), not {number}")
    return commit.parents[number - 1]


def nested_head(path: str) -> str:
    repo = open_git_dir(os.path.join(path, ".git"), path)
    head = resolve_ref(repo, "HEAD")
    if head is None:
        raise GitError(f"{path} is an embedded repository with no commit checked out; commit in it first or remove it")
    return head


def write_tree_from_dir(repo: Repo, path: str, keep_empty: bool = False) -> str:
    return snapshot(repo, path, keep_empty) or write_object(repo, "tree", b"")


def snapshot(repo: Repo, path: str, keep_empty: bool) -> Optional[str]:
    entries = []
    for name in sorted(os.listdir(path)):
        if name == ".git":
            continue
        full = os.path.join(path, name)
        info = os.lstat(full)
        if stat.S_ISLNK(info.st_mode):
            entries.append(TreeEntry(MODE_LINK, name, write_object(repo, "blob", os.fsencode(os.readlink(full)))))
        elif stat.S_ISDIR(info.st_mode) and os.path.exists(os.path.join(full, ".git")):
            entries.append(TreeEntry(MODE_GITLINK, name, nested_head(full)))
        elif stat.S_ISDIR(info.st_mode):
            sub = snapshot(repo, full, keep_empty)
            if sub:
                entries.append(TreeEntry(MODE_TREE, name, sub))
        elif stat.S_ISREG(info.st_mode):
            mode = MODE_EXEC if info.st_mode & stat.S_IXUSR else MODE_FILE
            with open(full, "rb") as handle:
                entries.append(TreeEntry(mode, name, write_object(repo, "blob", handle.read())))
    if not entries and not keep_empty:
        return None
    return write_object(repo, "tree", encode_tree(entries))


def parse_ls_tree_lines(text: str) -> List[TreeEntry]:
    entries = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        meta, tab, name = line.partition("\t")
        parts = meta.split()
        if not tab or len(parts) != 3:
            raise GitError(f"line {number}: expected 'MODE TYPE OID<TAB>NAME', got {line!r}")
        mode = parts[0].lstrip("0") or "0"
        if MODE_TYPES.get(mode) != parts[1]:
            raise GitError(f"line {number}: mode {parts[0]} does not hold a {parts[1]}")
        entries.append(TreeEntry(mode, name, parts[2]))
    return entries


def make_commit(repo: Repo, tree: str, parents: Sequence[str], message: str, env: Mapping[str, str], now: float) -> str:
    if read_object(repo, tree)[0] != "tree":
        raise GitError(f"{tree} is not a tree")
    for parent in parents:
        if read_object(repo, parent)[0] != "commit":
            raise GitError(f"parent {parent} is not a commit")
    commit = Commit(tree, list(parents), make_ident(repo, "AUTHOR", env, now), make_ident(repo, "COMMITTER", env, now), message)
    return write_object(repo, "commit", encode_commit(commit))


def commit_worktree(repo: Repo, message: str, env: Mapping[str, str], now: float, allow_empty: bool = False) -> Tuple[str, str]:
    if not repo.work_tree:
        raise GitError("a bare repository has no work tree to commit")
    text = cleanup_message(message)
    if not text:
        raise GitError("aborting commit due to empty commit message")
    tree = write_tree_from_dir(repo, repo.work_tree)
    parent = resolve_ref(repo, "HEAD")
    if parent and not allow_empty and parse_commit(read_object(repo, parent)[1]).tree == tree:
        raise GitError("nothing to commit: the work tree matches HEAD (use --allow-empty)")
    oid = make_commit(repo, tree, [parent] if parent else [], text, env, now)
    subject = text.split("\n", 1)[0]
    kind = "commit" if parent else "commit (initial)"
    ref = update_ref(repo, "HEAD", oid, make_ident(repo, "COMMITTER", env, now), old=parent or repo.zero, message=f"{kind}: {subject}")
    return oid, ref


def make_tag(repo: Repo, name: str, target: str, message: Optional[str], env: Mapping[str, str], now: float) -> str:
    ref = f"refs/tags/{name}"
    problem = check_ref_format(ref)
    if problem:
        raise GitError(f"'{name}' is not a valid tag name: {problem}")
    if read_ref(repo, ref) is not None:
        raise GitError(f"tag '{name}' already exists")
    oid = target
    if message is not None:
        kind = read_object(repo, target)[0]
        tagger = make_ident(repo, "COMMITTER", env, now)
        oid = write_object(repo, "tag", encode_tag(Tag(target, kind, name, tagger, cleanup_message(message))))
    write_locked(ref_path(repo, ref), (oid + "\n").encode(), lambda: None)
    return oid


def first_parent_log(repo: Repo, start: str, limit: int = 0) -> List[Tuple[str, Commit]]:
    out: List[Tuple[str, Commit]] = []
    current: Optional[str] = peel(repo, start, "commit")
    while current and (limit <= 0 or len(out) < limit):
        commit = parse_commit(read_object(repo, current)[1])
        out.append((current, commit))
        current = commit.parents[0] if commit.parents else None
    return out


def ensure_real_dir(path: str) -> None:
    if os.path.islink(path):
        raise GitError(f"refusing to write through the symbolic link {path}")
    if not os.path.exists(path):
        os.mkdir(path)
    elif not os.path.isdir(path):
        raise GitError(f"{path} exists and is not a directory")


def checkout_tree(repo: Repo, tree: str, target: str) -> int:
    if os.path.exists(target) and (not os.path.isdir(target) or os.listdir(target)):
        raise GitError(f"{target} is not empty; minigit checks out into a new or empty directory only")
    os.makedirs(target, exist_ok=True)
    return materialize(repo, tree, target)


def materialize(repo: Repo, tree: str, folder: str) -> int:
    kind, body = read_object(repo, tree)
    if kind != "tree":
        raise GitError(f"{tree} is a {kind}, not a tree")
    entries = parse_tree(body, repo.algo)
    unsafe = [d for check, d in tree_problems(entries) if check not in WARNING_CHECKS]
    if unsafe:
        raise GitError(f"refusing to check out tree {tree}: {'; '.join(unsafe)}")
    written = 0
    for entry in entries:
        path = os.path.join(folder, entry.name)
        if entry.mode == MODE_TREE:
            ensure_real_dir(path)
            written += materialize(repo, entry.oid, path)
        elif entry.mode == MODE_GITLINK:
            ensure_real_dir(path)
        elif entry.mode == MODE_LINK:
            os.symlink(os.fsdecode(read_object(repo, entry.oid)[1]), path)
            written += 1
        else:
            data = read_object(repo, entry.oid)[1]
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o777 if entry.mode == MODE_EXEC else 0o666)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            written += 1
    return written


def references(repo: Repo, kind: str, body: bytes) -> List[Tuple[str, str]]:
    if kind == "tree":
        return [(e.oid, e.kind) for e in parse_tree(body, repo.algo) if e.mode != MODE_GITLINK]
    if kind == "commit":
        commit = parse_commit(body)
        return [(commit.tree, "tree")] + [(p, "commit") for p in commit.parents]
    if kind == "tag":
        tag = parse_tag(body)
        return [(tag.object, tag.type)]
    return []


def object_problems(repo: Repo, kind: str, body: bytes) -> List[Tuple[str, str]]:
    if kind == "tree":
        try:
            return tree_problems(parse_tree(body, repo.algo))
        except GitError as error:
            return [("badTree", str(error))]
    if kind not in ("commit", "tag"):
        return []
    headers, _ = parse_headers(body)
    keys = [k for k, _ in headers]
    found: List[Tuple[str, str]] = []
    if kind == "commit":
        if not keys or keys[0] != "tree":
            found.append(("missingTree", "the first header is not 'tree'"))
        for key, value in headers:
            if key in ("tree", "parent") and (len(value) != repo.hex_len or not HEX.match(value)):
                found.append(("badTreeSha1" if key == "tree" else "badParentSha1", f"{key} {value!r} is not an object ID"))
        for role in ("author", "committer"):
            if role not in keys:
                found.append((f"missing{role.capitalize()}", f"no {role} header"))
        idents = [v for k, v in headers if k in ("author", "committer")]
    else:
        for key, check in (("object", "missingObject"), ("type", "missingTypeEntry"), ("tag", "missingTagEntry")):
            if key not in keys:
                found.append((check, f"no {key} header"))
        values = dict(headers)
        if "object" in values and (len(values["object"]) != repo.hex_len or not HEX.match(values["object"])):
            found.append(("badObjectSha1", f"object {values['object']!r} is not an object ID"))
        if "type" in values and values["type"] not in OBJECT_TYPES:
            found.append(("badType", f"type {values['type']!r}"))
        idents = [v for k, v in headers if k == "tagger"]
    for text in idents:
        problem = ident_problem(text)
        if problem:
            found.append(problem)
    return found


def verify_loose(repo: Repo, oid: str) -> Tuple[Optional[Tuple[str, bytes]], List[Problem]]:
    try:
        raw = read_raw(repo, oid)
    except GitError as error:
        return None, [Problem(oid, "?", "corruptObject", str(error))]
    actual = hashlib.new(repo.algo, raw).hexdigest()
    if actual != oid:
        return None, [Problem(oid, "?", "hashMismatch", f"content hashes to {actual}, but the file is named {oid}")]
    try:
        kind, body = unframe(raw)
    except GitError as error:
        return None, [Problem(oid, "?", "badObjectHeader", str(error))]
    return (kind, body), [Problem(oid, kind, check, detail) for check, detail in object_problems(repo, kind, body)]


def fsck_roots(repo: Repo) -> List[str]:
    roots = []
    head = resolve_ref(repo, "HEAD")
    if head:
        roots.append(head)
    roots += list(list_refs(repo).values())
    logs = os.path.join(repo.git_dir, "logs")
    for folder, _, files in os.walk(logs):
        for name in files:
            ref = os.path.relpath(os.path.join(folder, name), logs).replace(os.sep, "/")
            for entry in read_reflog(repo, ref):
                roots += [o for o in (entry.old, entry.new) if o != repo.zero]
    return list(dict.fromkeys(roots))


def fsck(repo: Repo) -> FsckReport:
    loose = loose_objects(repo)
    parsed: Dict[str, Tuple[str, bytes]] = {}
    problems: List[Problem] = []
    for oid in loose:
        result, found = verify_loose(repo, oid)
        problems += found
        if result:
            parsed[oid] = result
    edges: Dict[str, List[Tuple[str, str]]] = {}
    for oid, (kind, body) in parsed.items():
        try:
            edges[oid] = references(repo, kind, body)
        except GitError as error:
            edges[oid] = []
            problems.append(Problem(oid, kind, "badObject", str(error)))
    roots = fsck_roots(repo)
    reachable: Set[str] = set()
    absent: Set[str] = set()
    stack = [(oid, "") for oid in roots]
    while stack:
        oid, expected = stack.pop()
        if oid in reachable or oid in absent:
            continue
        if oid not in parsed:
            absent.add(oid)
            if oid not in loose:
                problems.append(Problem(oid, expected or "object", "missingObject", "referenced but not in the object store"))
            continue
        reachable.add(oid)
        kind = parsed[oid][0]
        if expected and kind != expected:
            problems.append(Problem(oid, kind, "wrongObjectType", f"referenced as a {expected}, but it is a {kind}"))
        stack.extend(edges.get(oid, []))
    unreachable = [o for o in parsed if o not in reachable]
    referenced = {child for o in unreachable for child, _ in edges.get(o, [])}
    dangling = [(o, parsed[o][0]) for o in unreachable if o not in referenced]
    return FsckReport(len(loose), len(reachable), len(roots), len(pack_files(repo)), problems, dangling)


def count_objects(repo: Repo) -> Dict[str, int]:
    count = 0
    size = 0
    for oid in loose_objects(repo):
        info = os.stat(object_path(repo, oid))
        blocks = getattr(info, "st_blocks", None)
        size += blocks * 512 if blocks is not None else info.st_size
        count += 1
    in_pack = 0
    for pack in pack_files(repo):
        idx = pack[:-len(".pack")] + ".idx"
        if os.path.isfile(idx):
            with open(idx, "rb") as handle:
                header = handle.read(8 + 256 * 4)
            if header[:4] == b"\xfftOc" and len(header) == 8 + 256 * 4:
                in_pack += int.from_bytes(header[-4:], "big")
    return {"count": count, "size": size // 1024, "in-pack": in_pack, "packs": len(pack_files(repo))}


def ident_json(ident: Optional[Ident]) -> Optional[Dict[str, Any]]:
    if ident is None:
        return None
    return {"name": ident.name, "email": ident.email, "time": ident.time, "tz": ident.tz, "date": iso_date(ident.time, ident.tz)}


def describe_object(repo: Repo, oid: str) -> Dict[str, Any]:
    kind, body = read_object(repo, oid)
    out: Dict[str, Any] = {"oid": oid, "type": kind, "size": len(body)}
    if kind == "blob":
        try:
            out["text"] = body.decode("utf-8")
        except UnicodeDecodeError:
            out["base64"] = base64.b64encode(body).decode("ascii")
    elif kind == "tree":
        out["entries"] = [tree_entry_json(e) for e in parse_tree(body, repo.algo)]
    elif kind == "commit":
        out.update(commit_json(oid, parse_commit(body)))
    else:
        tag = parse_tag(body)
        out.update({"object": tag.object, "object_type": tag.type, "tag": tag.tag, "tagger": ident_json(tag.tagger), "message": tag.message})
    return out


def tree_entry_json(entry: TreeEntry, path: str = "") -> Dict[str, str]:
    return {"mode": f"{int(entry.mode, 8):06o}" if entry.mode.isdigit() else entry.mode, "type": entry.kind, "oid": entry.oid, "path": path or entry.name}


def commit_json(oid: str, commit: Commit) -> Dict[str, Any]:
    return {"oid": oid, "tree": commit.tree, "parents": commit.parents, "author": ident_json(commit.author), "committer": ident_json(commit.committer), "message": commit.message}


def walk_tree(repo: Repo, tree: str, prefix: str = "") -> List[Tuple[TreeEntry, str]]:
    out = []
    for entry in parse_tree(read_object(repo, tree)[1], repo.algo):
        path = prefix + entry.name
        if entry.mode == MODE_TREE:
            out += walk_tree(repo, entry.oid, path + "/")
        else:
            out.append((entry, path))
    return out


def render_log(entries: Sequence[Tuple[str, Commit]], oneline: bool) -> str:
    lines = []
    for oid, commit in entries:
        subject = commit.message.split("\n", 1)[0]
        if oneline:
            lines.append(f"{oid[:7]} {subject}")
            continue
        lines.append(f"commit {oid}")
        if len(commit.parents) > 1:
            lines.append("Merge: " + " ".join(p[:7] for p in commit.parents))
        lines.append(f"Author: {commit.author.name} <{commit.author.email}>")
        lines.append(f"Date:   {format_date(commit.author.time, commit.author.tz)}")
        lines.append("")
        lines += ["    " + line for line in commit.message.rstrip("\n").split("\n")]
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def render_fsck(report: FsckReport, strict: bool) -> str:
    lines = []
    for p in report.problems:
        if p.check == "missingObject":
            lines.append(f"missing {p.kind} {p.oid}")
        else:
            level = "error" if strict or p.severity == "error" else "warning"
            lines.append(f"{level} in {p.kind} {p.oid}: {p.check}: {p.detail}")
    lines += [f"dangling {kind} {oid}" for oid, kind in report.dangling]
    errors = len(report.errors(strict))
    lines.append(f"minigit fsck: {report.loose} loose objects, {report.reachable} reachable from {report.roots} root(s), {errors} error(s), {len(report.problems) - errors} warning(s), {len(report.dangling)} dangling")
    if report.packs:
        lines.append(f"  ({report.packs} packfile(s) not checked: minigit reads loose objects only)")
    return "\n".join(lines)


def emit(data: bytes) -> None:
    sys.stdout.flush()
    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(data.decode("utf-8", "surrogateescape"))


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minigit", description=__doc__)
    parser.add_argument("-C", dest="cwd", default=".", metavar="PATH", help="run as if started in PATH")
    sub = parser.add_subparsers(dest="command", required=True)
    js = argparse.ArgumentParser(add_help=False)
    js.add_argument("--json", action="store_true", help="machine-readable output")
    init = sub.add_parser("init", help="create .git with HEAD, config, objects/ and refs/")
    init.add_argument("directory", nargs="?", default=".")
    init.add_argument("--object-format", default="sha1", choices=sorted(HASH_BYTES))
    init.add_argument("-b", "--initial-branch", default="main")
    init.add_argument("--bare", action="store_true")
    ho = sub.add_parser("hash-object", help="compute an object ID and optionally store the object")
    ho.add_argument("files", nargs="*")
    ho.add_argument("-t", dest="type", default="blob", choices=OBJECT_TYPES)
    ho.add_argument("-w", dest="write", action="store_true", help="write the object into the store")
    ho.add_argument("--stdin", action="store_true", help="read the content from standard input")
    ho.add_argument("--literally", action="store_true", help="skip the format checks for trees, commits and tags")
    ho.add_argument("--object-format", choices=sorted(HASH_BYTES), help="hash outside a repository")
    cat = sub.add_parser("cat-file", parents=[js], help="type, size or content of an object")
    mode = cat.add_mutually_exclusive_group(required=True)
    for flag, help_text in (("-t", "type"), ("-s", "size in bytes"), ("-p", "pretty-print"), ("-e", "exit 0 if the object exists")):
        mode.add_argument(flag, dest="mode", action="store_const", const=flag[1], help=help_text)
    mode.add_argument("--batch-check", dest="mode", action="store_const", const="batch", help="read names on stdin, print 'oid type size'")
    cat.add_argument("object", nargs="?")
    ls = sub.add_parser("ls-tree", parents=[js], help="list a tree like git ls-tree")
    ls.add_argument("rev")
    ls.add_argument("-r", dest="recursive", action="store_true")
    wt = sub.add_parser("write-tree", help="snapshot a directory into tree and blob objects")
    wt.add_argument("--path", default="", help="directory to snapshot (default: the work tree)")
    wt.add_argument("--keep-empty-dirs", action="store_true", help="store empty directories as empty trees (git add never does)")
    mk = sub.add_parser("mktree", help="build a tree from 'MODE TYPE OID<TAB>NAME' lines on stdin")
    mk.add_argument("--as-given", action="store_true", help="keep the input order instead of Git's order")
    mk.add_argument("--missing", action="store_true", help="allow entries whose objects are not in the store")
    ct = sub.add_parser("commit-tree", help="write a commit object for TREE")
    ct.add_argument("tree")
    ct.add_argument("-p", dest="parents", action="append", default=[])
    ct.add_argument("-m", dest="message", help="message (default: read standard input)")
    co = sub.add_parser("commit", help="snapshot the work tree and advance HEAD")
    co.add_argument("-m", dest="message", required=True)
    co.add_argument("--allow-empty", action="store_true")
    tg = sub.add_parser("tag", help="create a lightweight or annotated tag")
    tg.add_argument("name")
    tg.add_argument("rev", nargs="?", default="HEAD")
    tg.add_argument("-a", dest="annotate", action="store_true")
    tg.add_argument("-m", dest="message")
    ur = sub.add_parser("update-ref", help="point a ref at an object, optionally only if it holds OLD")
    ur.add_argument("ref")
    ur.add_argument("new", nargs="?")
    ur.add_argument("old", nargs="?")
    ur.add_argument("-m", dest="message", default="")
    ur.add_argument("-d", dest="delete", action="store_true")
    ur.add_argument("--no-deref", action="store_true")
    sr = sub.add_parser("symbolic-ref", help="read or set a symbolic ref such as HEAD")
    sr.add_argument("name")
    sr.add_argument("target", nargs="?")
    sr.add_argument("-m", dest="message", default="")
    rp = sub.add_parser("rev-parse", help="resolve names, ~N, ^N, ^{type}, @{N} and REV:path to object IDs")
    rp.add_argument("revs", nargs="+")
    sh = sub.add_parser("show-ref", parents=[js], help="list refs and the objects they name")
    sh.add_argument("--head", action="store_true")
    rl = sub.add_parser("reflog", parents=[js], help="previous values of a ref, newest first")
    rl.add_argument("ref", nargs="?", default="HEAD")
    lg = sub.add_parser("log", parents=[js], help="walk first parents from REV")
    lg.add_argument("rev", nargs="?", default="HEAD")
    lg.add_argument("-n", dest="limit", type=int, default=0)
    lg.add_argument("--oneline", action="store_true")
    ck = sub.add_parser("checkout", help="write the tree of REV into a new or empty directory")
    ck.add_argument("rev")
    ck.add_argument("directory")
    fk = sub.add_parser("fsck", parents=[js], help="verify every loose object and the connectivity from refs and reflogs")
    fk.add_argument("--strict", action="store_true", help="count warnings such as zero-padded modes as errors")
    sub.add_parser("count-objects", parents=[js], help="loose object count and disk usage, like git count-objects -v")
    return parser


def run(args: argparse.Namespace, stdin: BinaryIO, env: Mapping[str, str], now: float) -> int:
    if args.command == "init":
        target = os.path.join(args.cwd, args.directory)
        repo, existed = init_repo(target, args.object_format, args.initial_branch, args.bare)
        print(f"{'Reinitialized existing' if existed else 'Initialized empty'} minigit repository in {repo.git_dir}")
        return 0
    if args.command == "hash-object" and not args.write:
        try:
            repo = find_repo(args.cwd)
        except GitError:
            repo = Repo("", None, "sha1")
        if args.object_format:
            repo = Repo(repo.git_dir, repo.work_tree, args.object_format, repo.config)
    else:
        repo = find_repo(args.cwd)
    if args.command == "hash-object":
        blobs = [stdin.read()] if args.stdin else []
        for name in args.files:
            with open(os.path.join(args.cwd, name), "rb") as handle:
                blobs.append(handle.read())
        for body in blobs:
            problems = [] if args.literally else [d for c, d in object_problems(repo, args.type, body) if c not in WARNING_CHECKS]
            if args.type in ("commit", "tag") and not args.literally:
                try:
                    (parse_commit if args.type == "commit" else parse_tag)(body)
                except GitError as error:
                    problems.append(str(error))
            if problems:
                raise GitError(f"refusing to hash a malformed {args.type}: {'; '.join(problems)} (use --literally)")
            print(write_object(repo, args.type, body) if args.write else hash_object(args.type, body, repo.algo))
        return 0
    if args.command == "cat-file":
        return run_cat_file(repo, args, stdin)
    if args.command == "ls-tree":
        tree = peel(repo, rev_parse(repo, args.rev), "tree")
        if args.recursive:
            listing = walk_tree(repo, tree)
        else:
            listing = [(e, e.name) for e in parse_tree(read_object(repo, tree)[1], repo.algo)]
        if args.json:
            print_json([tree_entry_json(e, p) for e, p in listing])
        else:
            for entry, path in listing:
                print(ls_tree_line(entry, path))
        return 0
    if args.command == "write-tree":
        root = os.path.join(args.cwd, args.path) if args.path else repo.work_tree
        if not root:
            raise GitError("a bare repository has no work tree; pass --path DIR")
        print(write_tree_from_dir(repo, root, args.keep_empty_dirs))
        return 0
    if args.command == "mktree":
        entries = parse_ls_tree_lines(stdin.read().decode("utf-8", "surrogateescape"))
        if not args.missing:
            absent = [e.oid for e in entries if e.mode != MODE_GITLINK and not has_object(repo, e.oid)]
            if absent:
                raise GitError(f"entry object(s) not in the store: {', '.join(absent)} (use --missing)")
        print(write_object(repo, "tree", encode_tree(entries, keep_order=args.as_given)))
        return 0
    if args.command == "commit-tree":
        message = complete_line(args.message) if args.message is not None else stdin.read().decode("utf-8", "surrogateescape")
        tree = peel(repo, rev_parse(repo, args.tree), "tree")
        parents = [peel(repo, rev_parse(repo, p), "commit") for p in args.parents]
        print(make_commit(repo, tree, parents, message, env, now))
        return 0
    if args.command == "commit":
        oid, ref = commit_worktree(repo, args.message, env, now, args.allow_empty)
        branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else "detached HEAD"
        root = "" if parse_commit(read_object(repo, oid)[1]).parents else " (root-commit)"
        print(f"[{branch}{root} {oid[:7]}] {cleanup_message(args.message).splitlines()[0]}")
        return 0
    if args.command == "tag":
        if args.annotate and args.message is None:
            raise GitError("an annotated tag needs -m MESSAGE")
        message = args.message if (args.annotate or args.message is not None) else None
        print(make_tag(repo, args.name, rev_parse(repo, args.rev), message, env, now))
        return 0
    if args.command == "update-ref":
        if args.delete:
            delete_ref(repo, args.ref, rev_parse(repo, args.new) if args.new else None)
            return 0
        if not args.new:
            raise GitError("update-ref needs a new value (or -d)")
        new = rev_parse(repo, args.new)
        if not has_object(repo, new):
            raise GitError(f"{new} is not in the object store; refusing to point {args.ref} at it")
        old = None if args.old is None else (repo.zero if args.old in ("", repo.zero) else rev_parse(repo, args.old))
        update_ref(repo, args.ref, new, make_ident(repo, "COMMITTER", env, now), old, args.message, not args.no_deref)
        return 0
    if args.command == "symbolic-ref":
        if args.target is None:
            target = read_ref(repo, args.name)
            if not target or not target.startswith("ref: "):
                print(f"minigit: ref {args.name} is not a symbolic ref", file=sys.stderr)
                return 1
            print(target[len("ref: "):])
            return 0
        symbolic_ref(repo, args.name, args.target, make_ident(repo, "COMMITTER", env, now), args.message)
        return 0
    if args.command == "rev-parse":
        for rev in args.revs:
            base = re.split(r"[\^~:@]", rev, 1)[0]
            if base and len(dwim_refs(repo, base)) > 1:
                print(f"minigit: warning: refname '{base}' is ambiguous: {', '.join(dwim_refs(repo, base))}; using the first", file=sys.stderr)
            print(rev_parse(repo, rev))
        return 0
    if args.command == "show-ref":
        refs = list_refs(repo)
        if args.head and resolve_ref(repo, "HEAD"):
            refs = {"HEAD": resolve_ref(repo, "HEAD") or "", **refs}
        if args.json:
            print_json(refs)
        else:
            for name, oid in refs.items():
                print(f"{oid} {name}")
        return 0 if refs else 1
    if args.command == "reflog":
        ref = "HEAD" if args.ref == "HEAD" else follow_symrefs(repo, (dwim_refs(repo, args.ref) or [args.ref])[0])[0]
        entries = list(reversed(read_reflog(repo, ref)))
        rows = [{"selector": f"{args.ref}@{{{i}}}", "old": e.old, "new": e.new, "ident": ident_json(e.ident), "message": e.message} for i, e in enumerate(entries)]
        if args.json:
            print_json(rows)
        else:
            for row in rows:
                print(f"{row['new'][:7]} {row['selector']}: {row['message']}")
        return 0
    if args.command == "log":
        entries = first_parent_log(repo, rev_parse(repo, args.rev), args.limit)
        if args.json:
            print_json([commit_json(oid, c) for oid, c in entries])
        else:
            print(render_log(entries, args.oneline))
        return 0
    if args.command == "checkout":
        tree = peel(repo, rev_parse(repo, args.rev), "tree")
        target = os.path.join(args.cwd, args.directory)
        written = checkout_tree(repo, tree, target)
        print(f"checked out tree {tree} into {target}: {written} file(s)")
        return 0
    if args.command == "fsck":
        report = fsck(repo)
        if args.json:
            print_json({"ok": not report.errors(args.strict), "loose": report.loose, "reachable": report.reachable, "roots": report.roots, "packs": report.packs,
                        "problems": [{"oid": p.oid, "type": p.kind, "check": p.check, "severity": p.severity, "detail": p.detail} for p in report.problems],
                        "dangling": [{"oid": o, "type": k} for o, k in report.dangling]})
        else:
            print(render_fsck(report, args.strict))
        return 1 if report.errors(args.strict) else 0
    counts = count_objects(repo)
    if args.json:
        print_json(counts)
    else:
        for key in ("count", "size", "in-pack", "packs"):
            print(f"{key}: {counts[key]}")
    return 0


def run_cat_file(repo: Repo, args: argparse.Namespace, stdin: BinaryIO) -> int:
    if args.mode == "batch":
        for raw in stdin.read().decode("utf-8", "surrogateescape").splitlines():
            name = raw.strip()
            if not name:
                continue
            try:
                oid = rev_parse(repo, name)
                kind, body = read_object(repo, oid)
                print(f"{oid} {kind} {len(body)}")
            except GitError:
                print(f"{name} missing")
        return 0
    if not args.object:
        raise GitError("cat-file needs an object name")
    if args.mode == "e":
        try:
            read_object(repo, rev_parse(repo, args.object))
        except GitError:
            return 1
        return 0
    oid = rev_parse(repo, args.object)
    kind, body = read_object(repo, oid)
    if args.json:
        print_json(describe_object(repo, oid))
    elif args.mode == "t":
        print(kind)
    elif args.mode == "s":
        print(len(body))
    elif kind == "tree":
        for entry in parse_tree(body, repo.algo):
            print(ls_tree_line(entry))
    else:
        emit(body)
    return 0


def main(argv: Optional[Sequence[str]] = None, stdin: Optional[BinaryIO] = None, env: Optional[Mapping[str, str]] = None, now: Optional[float] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return run(args, stdin or sys.stdin.buffer, os.environ if env is None else env, time.time() if now is None else now)
    except GitError as error:
        print(f"minigit: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"minigit: {error.filename or ''}: {error.strerror}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
