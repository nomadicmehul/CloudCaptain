"""minihooks: a Git hook manager from scratch: a pre-commit config subset, staged-only runs with unstaged changes set aside, parallel hooks, Conventional Commits and a server-side pre-receive policy."""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Pattern, Sequence, Set, Tuple

CONFIG_FILE = ".pre-commit-config.yaml"
STAGES = ("pre-commit", "pre-merge-commit", "pre-push", "prepare-commit-msg", "commit-msg", "post-checkout",
          "post-commit", "post-merge", "post-rewrite", "pre-rebase", "manual")
STAGE_ALIASES = {"commit": "pre-commit", "merge-commit": "pre-merge-commit", "push": "pre-push"}
LANGUAGES = {"system": "system", "unsupported": "system", "script": "script", "unsupported_script": "script",
             "fail": "fail", "pygrep": "pygrep"}
HOOK_KEYS = {"id", "name", "entry", "language", "files", "exclude", "types", "types_or", "exclude_types", "stages", "args",
             "pass_filenames", "always_run", "require_serial", "verbose", "description", "alias"}
ROOT_KEYS = {"repos", "files", "exclude", "default_stages", "fail_fast", "minimum_pre_commit_version", "default_language_version", "ci"}
INSTALLABLE = ("pre-commit", "commit-msg", "pre-push", "pre-receive")
MARKER = "minihooks-shim"
WIDTH = 79
TEXT_BYTES = bytes(sorted({7, 8, 9, 10, 11, 12, 13, 27} | set(range(0x20, 0x100)) - {0x7F}))
EXTENSIONS = {
    "py": {"text", "python"}, "pyi": {"text", "python", "pyi"}, "sh": {"text", "shell", "sh"}, "bash": {"text", "shell", "bash"},
    "json": {"text", "json"}, "yaml": {"text", "yaml"}, "yml": {"text", "yaml"}, "md": {"text", "markdown"},
    "toml": {"text", "toml"}, "txt": {"text", "plain-text"}, "ini": {"text", "ini"}, "cfg": {"text", "ini"},
    "js": {"text", "javascript"}, "ts": {"text", "ts"}, "go": {"text", "go"}, "tf": {"text", "terraform"},
    "html": {"text", "html"}, "css": {"text", "css"}, "csv": {"text", "csv"}, "sql": {"text", "sql"},
    "png": {"binary", "image", "png"}, "jpg": {"binary", "image", "jpeg"}, "jpeg": {"binary", "image", "jpeg"},
    "gif": {"binary", "image", "gif"}, "zip": {"binary", "zip"}, "gz": {"binary", "gzip"}, "pdf": {"binary", "pdf"},
}
NAMES = {"Dockerfile": {"text", "dockerfile"}, "Makefile": {"text", "makefile"}, "Containerfile": {"text", "dockerfile"}}
INTERPRETERS = {"python": {"python"}, "python3": {"python", "python3"}, "bash": {"shell", "bash"}, "sh": {"shell", "sh"}}
COMMIT_TYPES = ("build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor", "revert", "style", "test")
HEADER = re.compile(r"^(?P<type>[^\s(!:]+)(?P<scope>\([^()]*\))?(?P<bang>!)?: (?P<description>.*)$")
FOOTER = re.compile(r"^(?P<token>BREAKING CHANGE|BREAKING-CHANGE|[A-Za-z][A-Za-z-]*)(?::\s|\s#)(?P<value>.*)$")
EXEMPT = re.compile(r"^(?:Merge |Revert \"|fixup! |squash! |amend! )")
SCISSORS = "------------------------ >8 ------------------------"
SECRET_PATTERNS = (
    ("aws-access-key-id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("private-key", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"),
    ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    ("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("assigned-secret", r"(?i)\b(?:password|passwd|secret|api_?key|token)\b\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"),
)
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class ConfigError(Exception):
    pass


class GitError(Exception):
    pass


class HookError(Exception):
    pass


@dataclass
class Line:
    number: int
    indent: int
    text: str


def strip_comment(text: str) -> str:
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote == "'":
            if char == "'":
                if text[index + 1:index + 2] == "'":
                    index += 1
                else:
                    quote = ""
        elif quote == '"':
            if char == "\\":
                index += 1
            elif char == '"':
                quote = ""
        elif char in "'\"" and (index == 0 or text[index - 1] in " \t[{,"):
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
        index += 1
    return text.rstrip()


def yaml_lines(source: str) -> List[Line]:
    lines = []
    for number, raw in enumerate(source.splitlines(), 1):
        if raw.rstrip() in ("---", "..."):
            continue
        body = strip_comment(raw)
        if not body.strip():
            continue
        stripped = body.lstrip(" ")
        if stripped.startswith("\t"):
            raise ConfigError(f"line {number}: tabs are not allowed for indentation")
        lines.append(Line(number, len(body) - len(stripped), stripped))
    return lines


def is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def split_key(text: str) -> Optional[Tuple[str, str]]:
    found = re.match(r"""^(?P<key>"[^"]*"|'[^']*'|[^'"\s#\[{-][^:]*?|-[^\s:][^:]*?)\s*:(?:\s+(?P<rest>.*))?$""", text)
    if not found:
        return None
    key = found.group("key")
    if key[:1] in "'\"":
        key = key[1:-1]
    return key, (found.group("rest") or "").strip()


def parse_yaml(source: str) -> Any:
    lines = yaml_lines(source)
    if not lines:
        return None
    value, index = parse_node(lines, 0, lines[0].indent)
    if index != len(lines):
        raise ConfigError(f"line {lines[index].number}: unexpected indentation")
    return value


def parse_node(lines: List[Line], index: int, indent: int) -> Tuple[Any, int]:
    if is_item(lines[index].text):
        return parse_sequence(lines, index, indent)
    if split_key(lines[index].text) is not None:
        return parse_mapping(lines, index, indent)
    return parse_scalar(lines[index].text, lines[index].number), index + 1


def parse_sequence(lines: List[Line], index: int, indent: int) -> Tuple[List[Any], int]:
    items: List[Any] = []
    while index < len(lines) and lines[index].indent == indent and is_item(lines[index].text):
        line = lines[index]
        rest = line.text[1:].lstrip(" ")
        if rest:
            child = indent + len(line.text) - len(rest)
            lines[index] = Line(line.number, child, rest)
            value, index = parse_node(lines, index, child)
        elif index + 1 < len(lines) and lines[index + 1].indent > indent:
            value, index = parse_node(lines, index + 1, lines[index + 1].indent)
        else:
            value, index = None, index + 1
        items.append(value)
    return items, index


def parse_mapping(lines: List[Line], index: int, indent: int) -> Tuple[Dict[str, Any], int]:
    result: Dict[str, Any] = {}
    while index < len(lines) and lines[index].indent == indent and not is_item(lines[index].text):
        line = lines[index]
        pair = split_key(line.text)
        if pair is None:
            raise ConfigError(f"line {line.number}: expected `key: value`, got {line.text!r}")
        key, rest = pair
        if key in result:
            raise ConfigError(f"line {line.number}: duplicate key {key!r}")
        index += 1
        if rest:
            result[key] = parse_scalar(rest, line.number)
        elif index < len(lines) and (lines[index].indent > indent or (lines[index].indent == indent and is_item(lines[index].text))):
            result[key], index = parse_node(lines, index, lines[index].indent)
        else:
            result[key] = None
    return result, index


def split_flow(body: str, number: int) -> List[str]:
    parts, current, quote = [], "", ""
    for char in body:
        if quote:
            current += char
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
            current += char
        elif char in "[]{}":
            raise ConfigError(f"line {number}: nested flow collections are not supported")
        elif char == ",":
            parts.append(current)
            current = ""
        else:
            current += char
    if quote:
        raise ConfigError(f"line {number}: unterminated quote in a flow sequence")
    parts.append(current)
    return [part for part in (p.strip() for p in parts) if part]


def parse_scalar(text: str, number: int) -> Any:
    text = text.strip()
    if text.startswith("["):
        if not text.endswith("]"):
            raise ConfigError(f"line {number}: unterminated flow sequence")
        return [parse_scalar(part, number) for part in split_flow(text[1:-1], number)]
    if text == "{}":
        return {}
    if text[:1] in "{&*!%@`":
        raise ConfigError(f"line {number}: {text[:1]!r} (flow mappings, anchors, tags) is outside the supported YAML subset")
    if text[:1] in "|>":
        raise ConfigError(f"line {number}: block scalars (| and >) are outside the supported YAML subset")
    if text.startswith("'"):
        if len(text) < 2 or not text.endswith("'"):
            raise ConfigError(f"line {number}: unterminated single-quoted string")
        return text[1:-1].replace("''", "'")
    if text.startswith('"'):
        try:
            return json.loads(text)
        except ValueError as error:
            raise ConfigError(f"line {number}: bad double-quoted string: {error}") from None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if text in ("null", "Null", "NULL", "~"):
        return None
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    return text


@dataclass
class Hook:
    id: str
    name: str
    entry: str
    language: str
    files: str = ""
    exclude: str = "^$"
    types: List[str] = field(default_factory=lambda: ["file"])
    types_or: List[str] = field(default_factory=list)
    exclude_types: List[str] = field(default_factory=list)
    stages: List[str] = field(default_factory=list)
    args: List[str] = field(default_factory=list)
    pass_filenames: bool = True
    always_run: bool = False
    require_serial: bool = False
    verbose: bool = False

    @property
    def exclusive(self) -> bool:
        return not self.pass_filenames or self.always_run or self.require_serial


@dataclass
class Config:
    hooks: List[Hook]
    files: str = ""
    exclude: str = "^$"
    default_stages: List[str] = field(default_factory=lambda: list(STAGES))
    warnings: List[str] = field(default_factory=list)


def as_list(value: Any, where: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where}: expected a list of strings, got {value!r}")
    return value


def as_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true or false, got {value!r}")
    return value


def as_regex(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected a regular expression string, got {value!r}")
    try:
        re.compile(value)
    except re.error as error:
        raise ConfigError(f"{where}: invalid regular expression {value!r}: {error}") from None
    return value


def as_stages(value: Any, where: str) -> List[str]:
    stages = [STAGE_ALIASES.get(stage, stage) for stage in as_list(value, where)]
    unknown = [stage for stage in stages if stage not in STAGES]
    if unknown:
        raise ConfigError(f"{where}: unknown stage(s) {', '.join(unknown)}; known: {', '.join(STAGES)}")
    return stages


def load_hook(raw: Any, where: str, warnings: List[str]) -> Hook:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping")
    for key in ("id", "name", "entry", "language"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ConfigError(f"{where}: missing required key `{key}`")
    where = f"hook {raw['id']}"
    language = LANGUAGES.get(raw["language"])
    if language is None:
        raise ConfigError(f"{where}: language {raw['language']!r} needs an environment minihooks does not build; use one of {', '.join(sorted(LANGUAGES))}")
    for key in sorted(set(raw) - HOOK_KEYS):
        warnings.append(f"{where}: unexpected key `{key}` ignored")
    hook = Hook(id=raw["id"], name=raw["name"], entry=raw["entry"], language=language)
    if "files" in raw:
        hook.files = as_regex(raw["files"], f"{where}.files")
    if "exclude" in raw:
        hook.exclude = as_regex(raw["exclude"], f"{where}.exclude")
    for key in ("types", "types_or", "exclude_types", "args"):
        if key in raw:
            setattr(hook, key, as_list(raw[key], f"{where}.{key}"))
    if "stages" in raw:
        hook.stages = as_stages(raw["stages"], f"{where}.stages")
    for key in ("pass_filenames", "always_run", "require_serial", "verbose"):
        if key in raw:
            setattr(hook, key, as_bool(raw[key], f"{where}.{key}"))
    if language == "pygrep":
        as_regex(hook.entry, f"{where}.entry")
    return hook


def load_config(source: str) -> Config:
    data = parse_yaml(source)
    if not isinstance(data, dict) or not isinstance(data.get("repos"), list):
        raise ConfigError("the configuration needs a top-level `repos:` list")
    config = Config(hooks=[])
    for key in sorted(set(data) - ROOT_KEYS):
        config.warnings.append(f"unexpected top-level key `{key}` ignored")
    if "files" in data:
        config.files = as_regex(data["files"], "files")
    if "exclude" in data:
        config.exclude = as_regex(data["exclude"], "exclude")
    if "default_stages" in data:
        config.default_stages = as_stages(data["default_stages"], "default_stages")
    for number, repo in enumerate(data["repos"], 1):
        if not isinstance(repo, dict):
            raise ConfigError(f"repos[{number}]: expected a mapping")
        if repo.get("repo") != "local":
            raise ConfigError(f"repos[{number}]: repo {repo.get('repo')!r}: minihooks runs only `repo: local` hooks; remote repositories need the clone-and-install step pre-commit does")
        hooks = repo.get("hooks")
        if not isinstance(hooks, list) or not hooks:
            raise ConfigError(f"repos[{number}]: `hooks:` must be a non-empty list")
        for index, raw in enumerate(hooks, 1):
            config.hooks.append(load_hook(raw, f"repos[{number}].hooks[{index}]", config.warnings))
    return config


def is_text(path: str) -> bool:
    with open(path, "rb") as handle:
        return not handle.read(1024).translate(None, TEXT_BYTES)


def shebang_tags(path: str) -> Set[str]:
    try:
        with open(path, "rb") as handle:
            first = handle.readline(256)
    except OSError:
        return set()
    if not first.startswith(b"#!"):
        return set()
    words = first[2:].decode("utf-8", "replace").split()
    if words and os.path.basename(words[0]) == "env":
        words = [word for word in words[1:] if not word.startswith("-")]
    if not words:
        return set()
    name = os.path.basename(words[0])
    return set(INTERPRETERS.get(name, INTERPRETERS.get(re.sub(r"[\d.]+$", "", name), set())))


def tags_for(path: str) -> Set[str]:
    if not os.path.lexists(path):
        return set()
    if os.path.islink(path):
        return {"symlink"}
    if os.path.isdir(path):
        return {"directory"}
    executable = os.access(path, os.X_OK)
    tags = {"file", "executable" if executable else "non-executable"}
    base = os.path.basename(path)
    found = set(NAMES.get(base, set()))
    if not found and "." in base.lstrip("."):
        found = set(EXTENSIONS.get(base.rsplit(".", 1)[1].lower(), set()))
    if not found and executable:
        found = shebang_tags(path)
    tags |= found
    if not tags & {"text", "binary"}:
        tags.add("text" if is_text(path) else "binary")
    return tags


def select_files(hook: Hook, candidates: Sequence[str], config: Config, root: str,
                 tags: Optional[Dict[str, Set[str]]] = None) -> List[str]:
    cache = tags if tags is not None else {}
    include, exclude = re.compile(hook.files), re.compile(hook.exclude)
    chosen = []
    for name in candidates:
        if not (re.search(config.files, name) and not re.search(config.exclude, name)):
            continue
        if not (include.search(name) and not exclude.search(name)):
            continue
        if name not in cache:
            cache[name] = tags_for(os.path.join(root, name))
        found = cache[name]
        if not found:
            continue
        if set(hook.types) <= found and (not hook.types_or or found & set(hook.types_or)) and not found & set(hook.exclude_types):
            chosen.append(name)
    return chosen


class Git:
    def __init__(self, cwd: str, env: Optional[Dict[str, str]] = None):
        self.cwd = cwd
        self.env = env

    def run(self, *args: str, data: Optional[bytes] = None) -> "subprocess.CompletedProcess[bytes]":
        try:
            return subprocess.run(["git", *args], cwd=self.cwd, env=self.env, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise GitError("git is not installed or not on PATH") from None

    def out(self, *args: str, data: Optional[bytes] = None) -> str:
        return self.out_bytes(*args, data=data).decode("utf-8", "replace")

    def out_bytes(self, *args: str, data: Optional[bytes] = None) -> bytes:
        proc = self.run(*args, data=data)
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.decode('utf-8', 'replace').strip()}")
        return proc.stdout

    def path(self, name: str) -> str:
        found = self.out("rev-parse", "--git-path", name).strip()
        return os.path.abspath(found if os.path.isabs(found) else os.path.join(self.cwd, found))


def zsplit(data: bytes) -> List[str]:
    return [part.decode("utf-8", "surrogateescape") for part in data.split(b"\0") if part]


def staged_files(git: Git) -> List[str]:
    return zsplit(git.out_bytes("diff", "--staged", "--name-only", "--no-ext-diff", "-z", "--diff-filter=ACMRTUXB"))


def tracked_files(git: Git) -> List[str]:
    return zsplit(git.out_bytes("ls-files", "-z", "--deduplicate"))


@dataclass
class StashReport:
    patch: str = ""
    conflicted: bool = False


@contextlib.contextmanager
def unstaged_changes_set_aside(git: Git, patch_dir: str, log: Callable[[str], None]) -> Iterator[StashReport]:
    report = StashReport()
    tree = git.out("write-tree").strip()
    diff = git.run("diff-index", "--ignore-submodules", "--binary", "--exit-code", "--no-color", "--no-ext-diff", tree, "--")
    if diff.returncode == 0 or (diff.returncode == 1 and not diff.stdout.strip()):
        yield report
        return
    if diff.returncode != 1:
        raise GitError(f"git diff-index failed: {diff.stderr.decode('utf-8', 'replace').strip()}")
    os.makedirs(patch_dir, exist_ok=True)
    report.patch = os.path.join(patch_dir, f"patch{int(time.time())}-{os.getpid()}")
    with open(report.patch, "wb") as handle:
        handle.write(diff.stdout)
    log("[WARNING] Unstaged files detected.")
    log(f"[INFO] Stashing unstaged files to {report.patch}.")
    git.out("-c", "submodule.recurse=0", "checkout", "--", ".")
    try:
        yield report
    finally:
        if git.run("apply", "--whitespace=nowarn", report.patch).returncode != 0:
            report.conflicted = True
            log("[WARNING] Stashed changes conflicted with hook auto-fixes... Rolling back fixes...")
            git.out("-c", "submodule.recurse=0", "checkout", "--", ".")
            git.out("apply", "--whitespace=nowarn", report.patch)
        log(f"[INFO] Restored changes from {report.patch}.")


@dataclass
class Job:
    index: int
    hook: Hook
    files: List[str]


@dataclass
class HookResult:
    id: str
    name: str
    status: str
    reason: str = ""
    exit_code: int = 0
    files: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    output: str = ""
    duration: float = 0.0
    after: List[str] = field(default_factory=list)


def conflicts(first: Job, second: Job) -> bool:
    return first.hook.exclusive or second.hook.exclusive or bool(set(first.files) & set(second.files))


def dependencies(jobs: Sequence[Job]) -> Dict[int, Set[int]]:
    return {later.index: {earlier.index for earlier in jobs[:position] if conflicts(earlier, later)}
            for position, later in enumerate(jobs)}


def changed_against_index(git: Git, root: str) -> Dict[str, str]:
    return {name: digest(os.path.join(root, name)) for name in zsplit(git.out_bytes("diff", "--name-only", "-z", "--no-ext-diff", "--ignore-submodules"))}


def digest(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return "missing"


def fingerprint(job: Job, git: Git, root: str) -> Dict[str, str]:
    if job.hook.exclusive:
        return changed_against_index(git, root)
    return {name: digest(os.path.join(root, name)) for name in job.files}


def pygrep(hook: Hook, files: Sequence[str], root: str) -> Tuple[int, str]:
    flags = re.IGNORECASE if "--ignore-case" in hook.args or "-i" in hook.args else 0
    multiline, negate = "--multiline" in hook.args, "--negate" in hook.args
    pattern = re.compile(hook.entry.encode(), flags | (re.MULTILINE | re.DOTALL if multiline else 0))
    code, out = 0, []
    for name in files:
        with open(os.path.join(root, name), "rb") as handle:
            content = handle.read()
        if multiline:
            found = pattern.search(content)
            hits = [] if not found else [(content[:found.start()].count(b"\n") + 1, content.split(b"\n")[content[:found.start()].count(b"\n")])]
        else:
            hits = [(number, line.rstrip(b"\r\n")) for number, line in enumerate(content.splitlines(True), 1) if pattern.search(line)]
        if negate:
            if not hits:
                code = 1
                out.append(name)
        elif hits:
            code = 1
            out.extend(f"{name}:{number}:{line.decode('utf-8', 'replace')}" for number, line in hits)
    return code, "\n".join(out) + ("\n" if out else "")


def execute(hook: Hook, files: Sequence[str], root: str, env: Dict[str, str], timeout: float) -> Tuple[int, str]:
    passed = list(files) if hook.pass_filenames else []
    if hook.language == "fail":
        return 1, f"{hook.entry}\n\n" + "".join(f"{name}\n" for name in passed)
    if hook.language == "pygrep":
        return pygrep(hook, passed, root)
    argv = shlex.split(hook.entry)
    if hook.language == "script":
        argv[0] = os.path.join(root, argv[0])
    argv += hook.args + passed
    try:
        proc = subprocess.run(argv, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    except FileNotFoundError:
        return 1, f"Executable `{argv[0]}` not found\n"
    except PermissionError:
        return 1, f"Executable `{argv[0]}` is not executable\n"
    except subprocess.TimeoutExpired as expired:
        partial = (expired.output or b"").decode("utf-8", "replace")
        return 1, f"{partial}hook timed out after {timeout:g}s\n"
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def run_job(job: Job, git: Git, root: str, env: Dict[str, str], timeout: float) -> HookResult:
    before = fingerprint(job, git, root)
    started = time.monotonic()
    code, output = execute(job.hook, job.files, root, env, timeout)
    duration = round(time.monotonic() - started, 3)
    after = fingerprint(job, git, root)
    modified = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    status = "Failed" if code or modified else "Passed"
    return HookResult(job.hook.id, job.hook.name, status, exit_code=code, files=list(job.files), modified=modified, output=output, duration=duration)


def schedule(jobs: Sequence[Job], deps: Dict[int, Set[int]], work: Callable[[Job], HookResult], workers: int) -> Dict[int, HookResult]:
    results: Dict[int, HookResult] = {}
    pending = {job.index: job for job in jobs}
    running: Dict[Future, int] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        while pending or running:
            ready = [index for index in sorted(pending) if deps[index] <= set(results)]
            for index in ready:
                running[pool.submit(work, pending.pop(index))] = index
            if not running:
                raise HookError("the hook schedule has a cycle")
            finished, _ = wait(list(running), return_when=FIRST_COMPLETED)
            for future in finished:
                results[running.pop(future)] = future.result()
    return results


@dataclass
class RunReport:
    stage: str
    results: List[HookResult]
    stash: Optional[StashReport] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(result.status == "Failed" for result in self.results)


def plan(config: Config, stage: str, candidates: Sequence[str], root: str, skips: Set[str], only: str = "") -> Tuple[List[HookResult], List[Job]]:
    placeholders: List[HookResult] = []
    jobs: List[Job] = []
    cache: Dict[str, Set[str]] = {}
    for hook in config.hooks:
        if stage not in (hook.stages or config.default_stages) or (only and hook.id != only):
            continue
        index = len(placeholders)
        files = select_files(hook, candidates, config, root, cache)
        if hook.id in skips:
            placeholders.append(HookResult(hook.id, hook.name, "Skipped", reason="SKIP"))
        elif not files and not hook.always_run:
            placeholders.append(HookResult(hook.id, hook.name, "Skipped", reason="(no files to check)"))
        else:
            placeholders.append(HookResult(hook.id, hook.name, "Pending", files=files))
            jobs.append(Job(index, hook, files))
    return placeholders, jobs


def run_hooks(root: str, config_path: str = CONFIG_FILE, stage: str = "pre-commit", all_files: bool = False,
              files: Sequence[str] = (), msg_file: str = "", workers: int = 4, timeout: float = 600.0,
              env: Optional[Dict[str, str]] = None, only: str = "", dry_run: bool = False,
              log: Callable[[str], None] = lambda message: print(message, file=sys.stderr)) -> RunReport:
    env = dict(os.environ if env is None else env)
    env["MINIHOOKS"] = "1"
    git = Git(root, env)
    stage = STAGE_ALIASES.get(stage, stage)
    if stage not in STAGES:
        raise ConfigError(f"unknown stage {stage!r}")
    if stage in ("commit-msg", "prepare-commit-msg") and not msg_file:
        raise ConfigError(f"--commit-msg-file is required for --hook-stage {stage}")
    stash = not all_files and not files and not dry_run
    if stash and git.out("ls-files", "--unmerged").strip():
        raise HookError("Unmerged files. Resolve before committing.")
    if stash and git.run("diff", "--quiet", "--no-ext-diff", "--", config_path).returncode != 0:
        raise HookError(f"Your hook configuration is unstaged. `git add {config_path}` to fix this.")
    with contextlib.ExitStack() as stack:
        stashed = stack.enter_context(unstaged_changes_set_aside(git, git.path("minihooks"), log)) if stash else None
        config_file = os.path.join(root, config_path)
        if not os.path.exists(config_file):
            raise ConfigError(f"{config_path} not found in {root}")
        with open(config_file, encoding="utf-8") as handle:
            config = load_config(handle.read())
        if msg_file:
            candidates = [os.path.relpath(os.path.abspath(msg_file), root)]
        elif files:
            candidates = [os.path.relpath(os.path.abspath(name), root) for name in files]
        elif all_files:
            candidates = tracked_files(git)
        else:
            candidates = staged_files(git)
        skips = {item.strip() for item in env.get("SKIP", "").split(",") if item.strip()}
        results, jobs = plan(config, stage, candidates, root, skips, only)
        deps = dependencies(jobs)
        by_id = {job.index: job.hook.id for job in jobs}
        for job in jobs:
            results[job.index].after = [by_id[index] for index in sorted(deps[job.index])]
        if not dry_run:
            finished = schedule(jobs, deps, lambda job: run_job(job, git, root, env, timeout), workers)
            for index, result in finished.items():
                result.after = results[index].after
                results[index] = result
        report = RunReport(stage, results, stashed, config.warnings)
    return report


def status_line(result: HookResult) -> str:
    postfix = result.reason if result.reason == "(no files to check)" else ""
    dots = max(3, WIDTH - len(result.name) - len(postfix) - len(result.status))
    return f"{result.name}{'.' * dots}{postfix}{result.status}"


def render(report: RunReport, dry_run: bool = False) -> str:
    out: List[str] = []
    for result in report.results:
        if dry_run:
            if result.status == "Skipped":
                out.append(f"{result.id}: skipped {result.reason}")
            else:
                when = "after " + ", ".join(result.after) if result.after else "start at once"
                out.append(f"{result.id}: {when}; {len(result.files)} file(s): {' '.join(result.files)}")
            continue
        out.append(status_line(result))
        if result.status == "Failed":
            out.append(f"- hook id: {result.id}")
            if result.exit_code:
                out.append(f"- exit code: {result.exit_code}")
            if result.modified:
                out.append("- files were modified by this hook")
        if result.output and (result.status == "Failed"):
            out.extend(["", result.output.rstrip("\n"), ""])
    return "\n".join(out) + "\n"


def clean_message(text: str, comment: str = "#") -> str:
    kept = []
    for line in text.splitlines():
        if line.startswith(f"{comment} {SCISSORS}"):
            break
        if line.startswith(comment):
            continue
        kept.append(line.rstrip())
    return "\n".join(kept).strip("\n")


@dataclass
class Message:
    header: str = ""
    type: str = ""
    scope: str = ""
    breaking: bool = False
    description: str = ""
    exempt: bool = False
    errors: List[str] = field(default_factory=list)


def check_message(text: str, types: Sequence[str] = COMMIT_TYPES, max_header: int = 100) -> Message:
    body = clean_message(text)
    if not body.strip():
        return Message(errors=["the commit message is empty"])
    lines = body.split("\n")
    message = Message(header=lines[0])
    if EXEMPT.match(message.header):
        message.exempt = True
        return message
    found = HEADER.match(message.header)
    if not found:
        message.errors.append(f"the header {message.header!r} is not `type(scope)!: description`")
    else:
        message.type = found.group("type")
        message.scope = (found.group("scope") or "")[1:-1]
        message.description = found.group("description")
        message.breaking = bool(found.group("bang"))
        if message.type.lower() not in [kind.lower() for kind in types]:
            message.errors.append(f"type {message.type!r} is not one of: {', '.join(types)}")
        if found.group("scope") is not None and not message.scope.strip():
            message.errors.append("the scope in parentheses is empty; name a scope or drop the parentheses")
        if not message.description.strip() or message.description[:1].isspace():
            message.errors.append("the description after `: ` is empty")
    if len(message.header) > max_header:
        message.errors.append(f"the header is {len(message.header)} characters; keep it to at most {max_header}")
    if len(lines) > 1 and lines[1].strip():
        message.errors.append("leave one blank line between the header and the body")
    for line in lines[1:]:
        footer = FOOTER.match(line)
        if footer and footer.group("token") in ("BREAKING CHANGE", "BREAKING-CHANGE"):
            message.breaking = True
        elif re.match(r"(?i)^breaking[ -]change:", line):
            message.errors.append("write the footer token as `BREAKING CHANGE:` in capitals")
    return message


@dataclass
class Policy:
    max_bytes: int = 1_000_000
    protected: List[str] = field(default_factory=lambda: ["refs/heads/main"])
    require_signed: bool = False
    require_conventional: bool = True
    types: List[str] = field(default_factory=lambda: list(COMMIT_TYPES))
    secrets: List[Tuple[str, Pattern]] = field(default_factory=lambda: [(name, re.compile(rx)) for name, rx in SECRET_PATTERNS])


@dataclass
class Violation:
    ref: str
    commit: str
    rule: str
    detail: str
    path: str = ""


def is_zero(sha: str) -> bool:
    return bool(sha) and set(sha) == {"0"}


def read_updates(text: str) -> List[Tuple[str, str, str]]:
    updates = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 3:
            raise HookError(f"pre-receive input line {number}: expected `<old> <new> <ref>`, got {line!r}")
        updates.append((parts[0], parts[1], parts[2]))
    return updates


def is_protected(ref: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(ref, pattern) for pattern in patterns)


def new_commits(git: Git, new: str) -> List[str]:
    return git.out("rev-list", "--topo-order", "--reverse", new, "--not", "--all").split()


def first_parent(git: Git, commit: str) -> Optional[str]:
    parents = git.out("rev-list", "--parents", "-n", "1", commit).split()[1:]
    return parents[0] if parents else None


def changed_blobs(git: Git, commit: str, parent: Optional[str]) -> List[Tuple[str, str]]:
    span = [parent, commit] if parent else ["--root", commit]
    tokens = zsplit(git.out_bytes("diff-tree", "-r", "-z", "--no-renames", "--no-commit-id", *span))
    blobs = []
    for meta, path in zip(tokens[0::2], tokens[1::2]):
        _, new_mode, _, new_sha, status = meta.lstrip(":").split()
        if status[0] in "AMT" and new_mode != "160000" and not is_zero(new_sha):
            blobs.append((path, new_sha))
    return blobs


def added_lines(git: Git, commit: str, parent: Optional[str]) -> Iterator[Tuple[str, int, str]]:
    span = [parent, commit] if parent else ["--root", commit]
    patch = git.out("-c", "core.quotePath=false", "diff-tree", "-p", "-U0", "--no-color", "--no-ext-diff", "--no-renames", "--no-commit-id", *span)
    path, number = "", 0
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@"):
            found = HUNK.match(line)
            number = int(found.group(1)) if found else 0
        elif line.startswith("+") and path:
            yield path, number, line[1:]
            number += 1


def check_commit(git: Git, ref: str, commit: str, policy: Policy, protected: bool) -> List[Violation]:
    found: List[Violation] = []
    parent = first_parent(git, commit)
    blobs = changed_blobs(git, commit, parent)
    if blobs:
        sizes = git.out("cat-file", "--batch-check=%(objectname) %(objectsize)", data="".join(f"{sha}\n" for _, sha in blobs).encode())
        size_of = dict(line.split() for line in sizes.splitlines() if len(line.split()) == 2)
        for path, sha in blobs:
            size = int(size_of.get(sha, "0"))
            if size > policy.max_bytes:
                found.append(Violation(ref, commit, "large-file", f"{path} is {size} bytes; the limit is {policy.max_bytes}", path))
    for path, number, text in added_lines(git, commit, parent):
        for name, pattern in policy.secrets:
            if pattern.search(text):
                found.append(Violation(ref, commit, "secret", f"{path}:{number} looks like a {name}", path))
    if protected and policy.require_signed:
        mark = git.out("log", "-1", "--format=%G?", commit).strip()
        if mark != "G":
            found.append(Violation(ref, commit, "unsigned", "the commit is not signed" if mark == "N" else f"the signature does not verify (%G? = {mark})"))
    if protected and policy.require_conventional:
        message = check_message(git.out("log", "-1", "--format=%B", commit), policy.types)
        for error in message.errors:
            found.append(Violation(ref, commit, "conventional-commit", error))
    return found


def check_push(git: Git, updates: Sequence[Tuple[str, str, str]], policy: Policy) -> List[Violation]:
    violations: List[Violation] = []
    for old, new, ref in updates:
        protected = is_protected(ref, policy.protected)
        if is_zero(new):
            if protected:
                violations.append(Violation(ref, old, "delete-protected", f"{ref} is protected and cannot be deleted"))
            continue
        if protected and not is_zero(old) and git.run("merge-base", "--is-ancestor", old, new).returncode != 0:
            violations.append(Violation(ref, new, "non-fast-forward", f"{ref} is protected; {old[:7]} is not an ancestor of {new[:7]}"))
        for commit in new_commits(git, new):
            violations.extend(check_commit(git, ref, commit, policy, protected))
    return violations


def policy_from_git(git: Git) -> Policy:
    policy = Policy()

    def get(*args: str) -> List[str]:
        proc = git.run("config", *args)
        return proc.stdout.decode().split("\n")[:-1] if proc.returncode == 0 else []

    size = get("--type=int", "--get", "minihooks.maxBytes")
    if size:
        policy.max_bytes = int(size[0])
    protect = get("--get-all", "minihooks.protect")
    if protect:
        policy.protected = protect
    for key, attribute in (("minihooks.requireSigned", "require_signed"), ("minihooks.requireConventional", "require_conventional")):
        value = get("--type=bool", "--get", key)
        if value:
            setattr(policy, attribute, value[0] == "true")
    kinds = get("--get", "minihooks.types")
    if kinds:
        policy.types = [kind.strip() for kind in kinds[0].split(",") if kind.strip()]
    return policy


def shim(hook_type: str, config: str) -> str:
    command = [sys.executable, os.path.abspath(__file__)]
    if hook_type == "pre-receive":
        command += ["pre-receive"]
        tail = ""
    elif hook_type == "commit-msg":
        command += ["run", "--hook-stage", hook_type, "--config", config, "--commit-msg-file"]
        tail = ' "$1"'
    else:
        command += ["run", "--hook-stage", hook_type, "--config", config]
        tail = ""
    return f"#!/bin/sh\n# {MARKER}\nexec {' '.join(shlex.quote(part) for part in command)}{tail}\n"


def install(repo: str, hook_types: Sequence[str], config: str = CONFIG_FILE, overwrite: bool = False) -> List[str]:
    git = Git(repo)
    hooks_dir = git.path("hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    written = []
    for hook_type in hook_types:
        if hook_type not in INSTALLABLE:
            raise HookError(f"cannot install {hook_type!r}; choose from {', '.join(INSTALLABLE)}")
        target = os.path.join(hooks_dir, hook_type)
        if os.path.exists(target) and not overwrite:
            with open(target, encoding="utf-8", errors="replace") as handle:
                if MARKER not in handle.read():
                    raise HookError(f"{target} exists and was not written by minihooks; move it away or pass --overwrite")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(shim(hook_type, config))
        os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        written.append(target)
    return written


SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sample")


def demo(directory: str, install_hooks: bool = True) -> Dict[str, str]:
    directory = os.path.abspath(directory)
    work, origin, bindir = (os.path.join(directory, name) for name in ("work", "origin.git", "bin"))
    if os.path.exists(work) or os.path.exists(origin):
        raise HookError(f"{directory} already holds a demo; choose an empty directory")
    os.makedirs(bindir, exist_ok=True)
    wrapper = os.path.join(bindir, "minihooks")
    with open(wrapper, "w", encoding="utf-8") as handle:
        handle.write(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))} \"$@\"\n")
    os.chmod(wrapper, 0o755)
    shutil.copytree(SAMPLE, work)
    env = dict(os.environ, GIT_AUTHOR_DATE="1790000000 +0000", GIT_COMMITTER_DATE="1790000000 +0000")
    git = Git(work, env)
    identity = ["-c", "user.name=Demo", "-c", "user.email=demo@example.invalid", "-c", "commit.gpgsign=false"]
    git.out("-c", "init.defaultBranch=main", "init", "-q")
    git.out("add", "-A")
    git.out(*identity, "commit", "-q", "--no-verify", "-m", "chore: start the sample project")
    Git(directory).out("-c", "init.defaultBranch=main", "init", "-q", "--bare", origin)
    server = Git(origin)
    server.out("config", "minihooks.maxBytes", "1m")
    server.out("config", "minihooks.protect", "refs/heads/main")
    server.out("config", "--add", "minihooks.protect", "refs/heads/release/*")
    install(origin, ["pre-receive"])
    git.out("remote", "add", "origin", origin)
    git.out("push", "-q", "origin", "main")
    if install_hooks:
        install(work, ["pre-commit", "commit-msg"])
    return {"work": work, "origin": origin, "bin": bindir}


def print_violations(violations: Sequence[Violation]) -> None:
    print(f"minihooks: push rejected, {len(violations)} problem(s)", file=sys.stderr)
    for item in violations:
        print(f"minihooks: {item.ref} {item.commit[:7]} {item.rule}: {item.detail}", file=sys.stderr)
    print("minihooks: fix the commits (git commit --amend, git rebase -i) and push again; a pushed secret must be revoked, not just deleted", file=sys.stderr)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="minihooks", description="A Git hook manager from scratch.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the hooks of one stage")
    run.add_argument("hook", nargs="?", default="", help="run only the hook with this id")
    run.add_argument("--config", default=CONFIG_FILE)
    run.add_argument("--hook-stage", default="pre-commit")
    run.add_argument("--all-files", action="store_true")
    run.add_argument("--files", nargs="+", default=[])
    run.add_argument("--commit-msg-file", default="")
    run.add_argument("--jobs", type=int, default=4)
    run.add_argument("--timeout", type=float, default=600.0)
    run.add_argument("--dry-run", action="store_true", help="print which hooks would run, on which files, in which order")
    run.add_argument("--json", action="store_true")
    ins = sub.add_parser("install", help="write hook shims into the repository's hooks directory")
    ins.add_argument("--repo", default=".")
    ins.add_argument("-t", "--hook-type", action="append", choices=INSTALLABLE)
    ins.add_argument("--config", default=CONFIG_FILE)
    ins.add_argument("--overwrite", action="store_true")
    msg = sub.add_parser("check-msg", help="validate a commit message file against Conventional Commits")
    msg.add_argument("file")
    msg.add_argument("--types", default=",".join(COMMIT_TYPES))
    msg.add_argument("--max-header", type=int, default=100)
    msg.add_argument("--json", action="store_true")
    rec = sub.add_parser("pre-receive", help="server-side policy; reads `<old> <new> <ref>` lines on stdin")
    rec.add_argument("--repo", default=".")
    rec.add_argument("--max-bytes", type=int)
    rec.add_argument("--protect", action="append")
    rec.add_argument("--require-signed", action=argparse.BooleanOptionalAction, default=None)
    rec.add_argument("--require-conventional", action=argparse.BooleanOptionalAction, default=None)
    rec.add_argument("--json", action="store_true")
    tag = sub.add_parser("tags", help="show the file type tags a hook's `types:` is matched against")
    tag.add_argument("paths", nargs="+")
    dem = sub.add_parser("demo", help="create a sample repository, a bare origin with a pre-receive policy, and install the hooks")
    dem.add_argument("directory")
    dem.add_argument("--no-install", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            root = Git(os.getcwd()).out("rev-parse", "--show-toplevel").strip()
            report = run_hooks(root, args.config, args.hook_stage, args.all_files, args.files, args.commit_msg_file,
                               args.jobs, args.timeout, only=args.hook, dry_run=args.dry_run)
            for warning in report.warnings:
                print(f"[WARNING] {warning}", file=sys.stderr)
            if args.json:
                print(json.dumps({"stage": report.stage, "failed": report.failed, "stash": asdict(report.stash) if report.stash else None,
                                  "hooks": [asdict(result) for result in report.results]}, indent=2))
            else:
                sys.stdout.write(render(report, args.dry_run))
            if args.hook and not report.results:
                print(f"No hook with id `{args.hook}` in stage `{args.hook_stage}`", file=sys.stderr)
                return 1
            return 1 if report.failed else 0
        if args.command == "install":
            for path in install(args.repo, args.hook_type or ["pre-commit"], args.config, args.overwrite):
                print(f"minihooks installed at {path}")
            return 0
        if args.command == "check-msg":
            with open(args.file, encoding="utf-8", errors="replace") as handle:
                message = check_message(handle.read(), [kind.strip() for kind in args.types.split(",") if kind.strip()], args.max_header)
            if args.json:
                print(json.dumps(asdict(message), indent=2))
            for error in message.errors:
                print(f"commit message: {error}", file=sys.stderr)
            if message.errors:
                print("commit message: expected e.g. `feat(api): add pagination` or `fix!: drop the v1 endpoint`", file=sys.stderr)
            return 1 if message.errors else 0
        if args.command == "pre-receive":
            git = Git(args.repo)
            policy = policy_from_git(git)
            if args.max_bytes is not None:
                policy.max_bytes = args.max_bytes
            if args.protect:
                policy.protected = args.protect
            if args.require_signed is not None:
                policy.require_signed = args.require_signed
            if args.require_conventional is not None:
                policy.require_conventional = args.require_conventional
            violations = check_push(git, read_updates(sys.stdin.read()), policy)
            if args.json:
                print(json.dumps([asdict(item) for item in violations], indent=2))
            if violations:
                print_violations(violations)
            return 1 if violations else 0
        if args.command == "tags":
            for path in args.paths:
                found = tags_for(path)
                if not found:
                    print(f"{path}: does not exist", file=sys.stderr)
                    return 2
                print(f"{path}: {' '.join(sorted(found))}")
            return 0
        if args.command == "demo":
            made = demo(args.directory, not args.no_install)
            print(f"sample repository  {made['work']}")
            print(f"bare origin        {made['origin']} (pre-receive installed; policy in its git config)")
            print(f"next: export PATH={shlex.quote(made['bin'])}:$PATH; cd {shlex.quote(made['work'])}")
            return 0
    except (ConfigError, GitError, HookError) as error:
        print(f"minihooks: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"minihooks: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
