"""minileaks: find leaked credentials in files, Git history and staged changes with provider rules, keyword prefilters and Shannon entropy, suppress known findings with allowlists, inline ignores and a baseline, and report them as text, JSON or SARIF with the rotation steps."""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

INLINE_IGNORE = ("gitleaks:allow", "minileaks:allow")
SUPPRESSION_REASONS = ("entropy", "invalid", "inline", "allowlist", "duplicate", "baseline")
REGEX_TARGETS = ("secret", "match", "line")
REDACTED_TAIL = "****"
BINARY_SNIFF = 8000
HOOK_MARKER = "MINILEAKS_HOOK=1"
GIT_LOG_FLAGS = ["--full-history", "--all", "--diff-filter=tuxdb"]
GIT_FORMAT_FLAGS = ["--no-color", "--no-ext-diff", "--no-decorate", "--date=iso-strict", "--format=medium"]
MESSAGE_FORMAT = "--format=%H%x1f%an%x1f%ae%x1f%ad%x1f%B"
COMMIT_LINE = re.compile(r"^commit ([0-9a-f]{40})(?: .*)?$")
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
AUTHOR_LINE = re.compile(r"^Author:\s*(.*?)\s*<([^>]*)>\s*$")
C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}

ROTATE_AWS = (
    "Deactivate the key first (aws iam update-access-key --user-name USER --access-key-id ID --status Inactive), "
    "find its owner with aws iam get-access-key-last-used --access-key-id ID, create and deploy a replacement, delete the old key, "
    "and search CloudTrail for the key ID from the commit date on (aws cloudtrail lookup-events --lookup-attributes AttributeKey=AccessKeyId,AttributeValue=ID)."
)
ROTATE_GITHUB = (
    "Revoke the token where it was issued (Settings, Developer settings for a personal token; the app's settings for an app or OAuth token), "
    "read the audit log of every organisation it could reach since the commit date, and replace it with a fine-grained token or the workflow's GITHUB_TOKEN."
)
ROTATE_SLACK = (
    "Revoke the token with the auth.revoke Web API method or by reinstalling the app, delete and recreate a leaked incoming webhook, "
    "and check the workspace access logs since the commit date."
)
ROTATE_KEY = (
    "Treat the key pair as compromised: generate a new pair, replace the public key wherever it is trusted (authorized_keys, deploy keys, "
    "certificates, which must also be revoked), then delete the old one."
)
ROTATE_JWT = (
    "A JWT is a bearer credential until it expires: revoke the session or refresh token behind it, and rotate the signing key if tokens "
    "never expire or the key itself may have leaked."
)
ROTATE_GENERIC = (
    "Change the value at the system that issued it, store the new one in a secret manager and read it at run time from the environment "
    "or a mounted file, never from the repository."
)
REMEDIATION_ORDER = [
    "Rotate or revoke every credential at its issuer now; history rewriting does not un-leak anything that was pushed or cloned.",
    "Read the issuer's logs for use of the old credential since the earliest commit date below.",
    "Remove the value from the code and load the new one from a secret store.",
    "Rewrite history: write the expressions with --replace-text FILE, run git filter-repo --replace-text FILE --replace-message FILE on a fresh clone, force-push every ref, and have everyone re-clone.",
    "Ask the host to purge cached views and pull request refs, then add any remaining accepted findings to the baseline.",
]

DEFAULT_RULES: List[Dict[str, Any]] = [
    {
        "id": "aws-access-token",
        "description": "AWS access key ID (long-term AKIA, temporary ASIA)",
        "regex": r"\b((?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16})\b",
        "keywords": ["a3t", "akia", "asia", "abia", "acca"],
        "entropy": 3.0,
        "rotate": ROTATE_AWS,
        "allowlists": [{"description": "AWS documentation example key", "regexes": [r".+EXAMPLE$"]}],
    },
    {
        "id": "aws-secret-access-key",
        "description": "AWS secret access key assigned next to an AWS keyword",
        "regex": r'''(?i)aws[A-Za-z0-9_.-]{0,20}(?:secret|sk)[A-Za-z0-9_.-]{0,20}[\s'"]{0,3}(?:=|:=|:|=>)[\s'"]{0,5}([A-Za-z0-9/+]{40})(?:[^A-Za-z0-9/+=]|$)''',
        "keywords": ["aws"],
        "entropy": 4.0,
        "rotate": ROTATE_AWS,
    },
    {
        "id": "github-pat",
        "description": "GitHub personal access token (classic)",
        "regex": r"ghp_[0-9a-zA-Z]{36}",
        "keywords": ["ghp_"],
        "entropy": 3.0,
        "rotate": ROTATE_GITHUB,
    },
    {
        "id": "github-fine-grained-pat",
        "description": "GitHub fine-grained personal access token",
        "regex": r"github_pat_[0-9a-zA-Z_]{82}",
        "keywords": ["github_pat_"],
        "entropy": 3.0,
        "rotate": ROTATE_GITHUB,
    },
    {
        "id": "github-oauth",
        "description": "GitHub OAuth access token",
        "regex": r"gho_[0-9a-zA-Z]{36}",
        "keywords": ["gho_"],
        "entropy": 3.0,
        "rotate": ROTATE_GITHUB,
    },
    {
        "id": "github-app-token",
        "description": "GitHub App user-to-server or server-to-server token",
        "regex": r"(?:ghu|ghs)_[0-9a-zA-Z]{36}",
        "keywords": ["ghu_", "ghs_"],
        "entropy": 3.0,
        "rotate": ROTATE_GITHUB,
    },
    {
        "id": "slack-bot-token",
        "description": "Slack bot token",
        "regex": r"xoxb-[0-9]{10,13}-[0-9]{10,13}[a-zA-Z0-9-]*",
        "keywords": ["xoxb"],
        "entropy": 3.0,
        "rotate": ROTATE_SLACK,
    },
    {
        "id": "slack-user-token",
        "description": "Slack user token",
        "regex": r"xox[pe](?:-[0-9]{10,13}){3}-[a-zA-Z0-9-]{28,34}",
        "keywords": ["xoxp-", "xoxe-"],
        "entropy": 2.0,
        "rotate": ROTATE_SLACK,
    },
    {
        "id": "slack-webhook-url",
        "description": "Slack incoming webhook URL",
        "regex": r"(?:https?://)?hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9+/]{43,56}",
        "keywords": ["hooks.slack.com"],
        "rotate": ROTATE_SLACK,
    },
    {
        "id": "private-key",
        "description": "PEM or OpenSSH private key block",
        "regex": r"(?i)-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----[\s\S-]{64,}?KEY(?: BLOCK)?-----",
        "keywords": ["-----begin"],
        "rotate": ROTATE_KEY,
    },
    {
        "id": "jwt",
        "description": "JSON Web Token whose header decodes to JSON with an alg",
        "regex": r'''\b(eyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.(?:[A-Za-z0-9_-]{10,})?)(?:['"\s;,)]|$)''',
        "keywords": ["eyj"],
        "entropy": 3.0,
        "validate": "jwt",
        "rotate": ROTATE_JWT,
    },
    {
        "id": "generic-password",
        "description": "Password, secret, token or key assigned a literal value",
        "regex": r'''(?i)\b[A-Za-z0-9_.-]{0,30}?(?:passw(?:or)?d|pwd|secret|token|api[_.-]?key|access[_.-]?key|auth[_.-]?key|credential)[A-Za-z0-9_.-]{0,20}['"]?[ \t]{0,3}(?:=|:=|:|=>)[ \t]{0,5}['"]?([A-Za-z0-9_!#$%&*+./=@^~-]{8,128})(?:['"\s;,)]|$)''',
        "keywords": ["passwd", "password", "pwd", "secret", "token", "api", "key", "credential"],
        "entropy": 3.0,
        "rotate": ROTATE_GENERIC,
        "allowlists": [
            {
                "description": "identifiers, attribute paths, variable references and template placeholders, not values",
                "regexes": [r"^[A-Za-z_.-]+$", r"^\$[A-Za-z_][A-Za-z0-9_]*$", r"^%[A-Za-z_][A-Za-z0-9_]*%$", r"^@[A-Za-z_][A-Za-z0-9_]*@$", r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"],
            }
        ],
    },
]

DEFAULT_ALLOWLISTS: List[Dict[str, Any]] = [
    {
        "description": "never scan Git's own store, lock files or binary assets",
        "paths": [
            r"(?:^|/)\.git$",
            r"(?:^|/)(?:package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|go\.sum)$",
            r"\.(?i:png|jpe?g|gif|ico|pdf|zip|gz|woff2?)$",
        ],
        "stopwords": ["example", "changeme", "placeholder", "dummy", "redacted", "xxxxxxxx", "your_", "your-"],
    }
]


class InputError(Exception):
    pass


def shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts: Dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    length = len(text)
    return 0.0 - sum((n / length) * math.log2(n / length) for n in counts.values())


def compile_all(patterns: Iterable[str], what: str) -> List["re.Pattern[str]"]:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.ASCII))
        except re.error as exc:
            raise InputError(f"{what}: bad regex {pattern!r}: {exc}")
    return compiled


@dataclass
class Allowlist:
    description: str = ""
    paths: List[str] = field(default_factory=list)
    regexes: List[str] = field(default_factory=list)
    regex_target: str = "secret"
    stopwords: List[str] = field(default_factory=list)
    commits: List[str] = field(default_factory=list)
    path_patterns: List["re.Pattern[str]"] = field(init=False, repr=False)
    regex_patterns: List["re.Pattern[str]"] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.regex_target not in REGEX_TARGETS:
            raise InputError(f"allowlist {self.description!r}: regex_target must be one of {', '.join(REGEX_TARGETS)}")
        if not (self.paths or self.regexes or self.stopwords or self.commits):
            raise InputError(f"allowlist {self.description!r}: needs at least one of paths, regexes, stopwords, commits")
        self.path_patterns = compile_all(self.paths, f"allowlist {self.description!r} paths")
        self.regex_patterns = compile_all(self.regexes, f"allowlist {self.description!r} regexes")
        self.stopwords = [w.lower() for w in self.stopwords]
        self.commits = [c.strip().lower() for c in self.commits]

    def path_allowed(self, path: str) -> bool:
        return bool(path) and any(p.search(path) for p in self.path_patterns)

    def commit_allowed(self, commit: str) -> bool:
        return bool(commit) and commit.lower() in self.commits

    def value_allowed(self, secret: str, match: str, line: str) -> bool:
        target = {"match": match, "line": line}.get(self.regex_target, secret)
        if target and any(p.search(target) for p in self.regex_patterns):
            return True
        lowered = secret.lower()
        return any(word in lowered for word in self.stopwords)


@dataclass
class Rule:
    id: str
    description: str
    regex: str
    keywords: List[str] = field(default_factory=list)
    entropy: float = 0.0
    secret_group: int = 0
    validate: str = ""
    rotate: str = ""
    allowlists: List[Allowlist] = field(default_factory=list)
    pattern: "re.Pattern[str]" = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.id or not self.regex:
            raise InputError("every rule needs an id and a regex")
        self.pattern = compile_all([self.regex], f"rule {self.id}")[0]
        self.keywords = [k.lower() for k in self.keywords]
        if self.secret_group > self.pattern.groups:
            raise InputError(f"rule {self.id}: secret_group {self.secret_group} but the regex has {self.pattern.groups} group(s)")
        if self.validate and self.validate not in VALIDATORS:
            raise InputError(f"rule {self.id}: unknown validator {self.validate!r} (known: {', '.join(sorted(VALIDATORS))})")


@dataclass
class Config:
    rules: List[Rule]
    allowlists: List[Allowlist]

    def rule(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(rule_id)


def valid_jwt(secret: str) -> bool:
    header = secret.split(".", 1)[0]
    try:
        decoded = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
    except (binascii.Error, ValueError):
        return False
    return isinstance(decoded, dict) and "alg" in decoded


VALIDATORS: Dict[str, Callable[[str], bool]] = {"jwt": valid_jwt}


def make_allowlist(data: Dict[str, Any]) -> Allowlist:
    known = {"description", "paths", "regexes", "regex_target", "stopwords", "commits"}
    unknown = set(data) - known
    if unknown:
        raise InputError(f"allowlist: unknown key(s) {', '.join(sorted(unknown))} (known: {', '.join(sorted(known))})")
    return Allowlist(**data)


def make_rule(data: Dict[str, Any]) -> Rule:
    known = {"id", "description", "regex", "keywords", "entropy", "secret_group", "validate", "rotate", "allowlists"}
    unknown = set(data) - known
    if unknown:
        raise InputError(f"rule {data.get('id', '?')}: unknown key(s) {', '.join(sorted(unknown))}")
    body = dict(data)
    body["allowlists"] = [make_allowlist(a) for a in data.get("allowlists") or []]
    body.setdefault("description", body.get("id", ""))
    return Rule(**body)


def build_config(data: Optional[Dict[str, Any]] = None) -> Config:
    data = data or {}
    use_default = data.get("use_default", True)
    rules: Dict[str, Rule] = {}
    for spec in (DEFAULT_RULES if use_default else []) + list(data.get("rules") or []):
        rule = make_rule(spec)
        rules.pop(rule.id, None)
        rules[rule.id] = rule
    disabled = set(data.get("disabled_rules") or [])
    allowlists = [make_allowlist(a) for a in (DEFAULT_ALLOWLISTS if use_default else []) + list(data.get("allowlists") or [])]
    selected = [r for r in rules.values() if r.id not in disabled]
    if not selected:
        raise InputError("the configuration has no rules")
    return Config(selected, allowlists)


def load_config(path: Optional[str]) -> Config:
    if not path:
        return build_config()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read config {path}: {exc.strerror}")
    except ValueError as exc:
        raise InputError(f"config {path} is not JSON: {exc}")
    if not isinstance(data, dict):
        raise InputError(f"config {path}: expected an object with rules, allowlists, use_default, disabled_rules")
    return build_config(data)


@dataclass
class Fragment:
    text: str
    path: str = ""
    start_line: int = 1
    commit: str = ""
    author: str = ""
    email: str = ""
    date: str = ""
    message: str = ""
    where: str = "file"


@dataclass
class Finding:
    rule: str
    description: str
    where: str
    file: str
    line: int
    end_line: int
    column: int
    end_column: int
    secret: str
    match: str
    line_text: str
    entropy: float
    commit: str = ""
    author: str = ""
    email: str = ""
    date: str = ""
    message: str = ""

    @property
    def fingerprint(self) -> str:
        if self.commit:
            return f"{self.commit}:{self.file}:{self.rule}:{self.line}"
        return f"{self.file}:{self.rule}:{self.line}"

    @property
    def secret_sha256(self) -> str:
        return hashlib.sha256(self.secret.encode("utf-8")).hexdigest()


@dataclass
class Stats:
    fragments: int = 0
    bytes: int = 0
    rule_runs: int = 0
    prefiltered: int = 0
    paths_skipped: int = 0
    commits: Set[str] = field(default_factory=set)
    files: Set[str] = field(default_factory=set)
    suppressed: Dict[str, int] = field(default_factory=lambda: {reason: 0 for reason in SUPPRESSION_REASONS})


def redact(secret: str) -> str:
    first_line = secret.split("\n", 1)[0]
    return first_line[:4] + REDACTED_TAIL


def locate(text: str, start: int, end: int) -> Tuple[int, int, int, int, int, int]:
    line_offset = text.count("\n", 0, start)
    line_start = text.rfind("\n", 0, start) + 1
    last = max(start, end - 1)
    end_offset = text.count("\n", 0, last)
    end_line_start = text.rfind("\n", 0, last) + 1
    line_end = text.find("\n", last)
    if line_end == -1:
        line_end = len(text)
    return line_offset, start - line_start + 1, end_offset, end - end_line_start, line_start, line_end


def pick_secret(match: "re.Match[str]", rule: Rule, whole: str) -> str:
    if rule.secret_group:
        return match.group(rule.secret_group) or ""
    for group in match.groups():
        if group:
            return group
    return whole


def fragment_allowed(fragment: Fragment, allowlists: Sequence[Allowlist]) -> bool:
    if not fragment.path and not fragment.commit:
        return False
    return any(a.path_allowed(fragment.path) or a.commit_allowed(fragment.commit) for a in allowlists)


def detect_rule(fragment: Fragment, rule: Rule, config: Config, stats: Stats, inline_ignore: bool) -> List[Finding]:
    if fragment_allowed(fragment, rule.allowlists):
        return []
    findings: List[Finding] = []
    for match in rule.pattern.finditer(fragment.text):
        whole = match.group(0).strip("\n")
        start = match.start() + (len(match.group(0)) - len(match.group(0).lstrip("\n")))
        end = start + len(whole)
        line_offset, column, end_offset, end_column, line_start, line_end = locate(fragment.text, start, end)
        line_text = fragment.text[line_start:line_end]
        if inline_ignore and any(marker in line_text for marker in INLINE_IGNORE):
            stats.suppressed["inline"] += 1
            continue
        secret = pick_secret(match, rule, whole)
        entropy = shannon_entropy(secret)
        if rule.entropy and entropy <= rule.entropy:
            stats.suppressed["entropy"] += 1
            continue
        if rule.validate and not VALIDATORS[rule.validate](secret):
            stats.suppressed["invalid"] += 1
            continue
        if any(a.value_allowed(secret, whole, line_text) for a in list(config.allowlists) + list(rule.allowlists)):
            stats.suppressed["allowlist"] += 1
            continue
        findings.append(
            Finding(
                rule.id, rule.description, fragment.where, fragment.path,
                fragment.start_line + line_offset, fragment.start_line + end_offset, column, end_column,
                secret, whole, line_text, round(entropy, 3),
                fragment.commit, fragment.author, fragment.email, fragment.date, fragment.message,
            )
        )
    return findings


def drop_generic_duplicates(findings: List[Finding], stats: Stats) -> List[Finding]:
    kept = []
    for finding in findings:
        shadowed = "generic" in finding.rule.lower() and any(
            other.line == finding.line
            and other.commit == finding.commit
            and other.rule != finding.rule
            and finding.secret in other.secret
            and "generic" not in other.rule.lower()
            for other in findings
        )
        if shadowed:
            stats.suppressed["duplicate"] += 1
        else:
            kept.append(finding)
    return kept


def detect(fragment: Fragment, config: Config, stats: Stats, inline_ignore: bool = True) -> List[Finding]:
    stats.fragments += 1
    stats.bytes += len(fragment.text.encode("utf-8"))
    if fragment.commit:
        stats.commits.add(fragment.commit)
    if fragment.path:
        stats.files.add(fragment.path)
    if fragment_allowed(fragment, config.allowlists):
        stats.paths_skipped += 1
        return []
    lowered = fragment.text.lower()
    findings: List[Finding] = []
    for rule in config.rules:
        if rule.keywords and not any(k in lowered for k in rule.keywords):
            stats.prefiltered += 1
            continue
        stats.rule_runs += 1
        findings.extend(detect_rule(fragment, rule, config, stats, inline_ignore))
    return drop_generic_duplicates(findings, stats)


@dataclass(frozen=True)
class BaselineEntry:
    rule: str
    file: str
    commit: str
    secret_sha256: Optional[str]
    fingerprint: str


def baseline_entry(item: Dict[str, Any]) -> BaselineEntry:
    if "RuleID" in item:
        secret = item.get("Secret") or ""
        redacted = secret == "REDACTED" or secret.endswith("...")
        digest = None if redacted or not secret else hashlib.sha256(secret.encode("utf-8")).hexdigest()
        return BaselineEntry(item["RuleID"], item.get("File") or "", item.get("Commit") or "", digest, item.get("Fingerprint") or "")
    if "rule" in item:
        return BaselineEntry(item["rule"], item.get("file") or "", item.get("commit") or "", item.get("secret_sha256"), item.get("fingerprint") or "")
    raise InputError(f"baseline entry has neither RuleID (gitleaks) nor rule (minileaks): {sorted(item)}")


class Baseline:
    def __init__(self, entries: Iterable[BaselineEntry]):
        self.keys: Set[Tuple[str, str, str, str]] = set()
        self.fingerprints: Set[str] = set()
        for entry in entries:
            if entry.secret_sha256:
                self.keys.add((entry.rule, entry.file, entry.commit, entry.secret_sha256))
            elif entry.fingerprint:
                self.fingerprints.add(entry.fingerprint)

    def __len__(self) -> int:
        return len(self.keys) + len(self.fingerprints)

    def contains(self, finding: Finding) -> bool:
        key = (finding.rule, finding.file, finding.commit, finding.secret_sha256)
        return key in self.keys or finding.fingerprint in self.fingerprints


def load_baseline(path: str) -> Baseline:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read baseline {path}: {exc.strerror}")
    except ValueError as exc:
        raise InputError(f"baseline {path} is not JSON: {exc}")
    items = data.get("findings") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise InputError(f"baseline {path}: expected a minileaks --json report or a gitleaks JSON report (a list of findings)")
    return Baseline(baseline_entry(item) for item in items)


def scan(fragments: Iterable[Fragment], config: Config, baseline: Optional[Baseline] = None, inline_ignore: bool = True, stats: Optional[Stats] = None) -> Tuple[List[Finding], Stats]:
    stats = stats or Stats()
    findings: List[Finding] = []
    for fragment in fragments:
        for finding in detect(fragment, config, stats, inline_ignore):
            if baseline is not None and baseline.contains(finding):
                stats.suppressed["baseline"] += 1
                continue
            findings.append(finding)
    return findings, stats


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:BINARY_SNIFF]


def dir_fragments(root: str, config: Config, stats: Stats) -> Iterator[Fragment]:
    if not os.path.exists(root):
        raise InputError(f"{root}: no such file or directory")
    if os.path.isfile(root):
        candidates: Iterable[str] = [os.path.normpath(root)]
    else:
        candidates = walk_files(root, config, stats)
    for path in candidates:
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        if not data or is_binary(data):
            continue
        yield Fragment(data.decode("utf-8", errors="replace"), path=path)


def walk_files(root: str, config: Config, stats: Stats) -> Iterator[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for name in sorted(dirnames):
            path = os.path.normpath(os.path.join(dirpath, name))
            if any(a.path_allowed(path) for a in config.allowlists):
                stats.paths_skipped += 1
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            path = os.path.normpath(os.path.join(dirpath, name))
            if os.path.islink(path):
                continue
            if any(a.path_allowed(path) for a in config.allowlists):
                stats.paths_skipped += 1
                continue
            yield path


def unquote_path(raw: str) -> str:
    raw = raw.rstrip("\t")
    if not (raw.startswith('"') and raw.endswith('"') and len(raw) >= 2):
        return raw
    body, out, i = raw[1:-1], bytearray(), 0
    while i < len(body):
        char = body[i]
        if char == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in C_ESCAPES:
                out.append(C_ESCAPES[nxt])
                i += 2
                continue
            if body[i + 1:i + 4].isdigit():
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
        out.extend(char.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def strip_side(path: str) -> str:
    return path[2:] if path[:2] in ("a/", "b/") else path


def to_utc(stamp: str) -> str:
    stamp = stamp.strip()
    if not stamp:
        return ""
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class CommitInfo:
    sha: str
    author: str = ""
    email: str = ""
    date: str = ""
    message: str = ""


def read_commit_header(lines: List[str], i: int) -> Tuple[CommitInfo, int]:
    info = CommitInfo(COMMIT_LINE.match(lines[i]).group(1))
    i += 1
    while i < len(lines) and lines[i] != "":
        author = AUTHOR_LINE.match(lines[i])
        if author:
            info.author, info.email = author.group(1), author.group(2)
        elif lines[i].startswith("Date:"):
            info.date = to_utc(lines[i][5:])
        i += 1
    i += 1
    message = []
    while i < len(lines) and lines[i].startswith("    "):
        message.append(lines[i][4:])
        i += 1
    info.message = "\n".join(message).strip("\n")
    if i < len(lines) and lines[i] == "":
        i += 1
    return info, i


def added_runs(lines: List[str], i: int, old_count: int, new_start: int, new_count: int) -> Tuple[List[Tuple[int, List[str]]], int]:
    runs: List[Tuple[int, List[str]]] = []
    seen_old = seen_new = 0
    line_no = new_start
    while i < len(lines) and (seen_old < old_count or seen_new < new_count):
        text = lines[i]
        if text.startswith("+"):
            if runs and runs[-1][0] + len(runs[-1][1]) == line_no:
                runs[-1][1].append(text[1:])
            else:
                runs.append((line_no, [text[1:]]))
            seen_new += 1
            line_no += 1
        elif text.startswith("-"):
            seen_old += 1
        elif text.startswith(" "):
            seen_old += 1
            seen_new += 1
            line_no += 1
        elif not text.startswith("\\"):
            break
        i += 1
    while i < len(lines) and lines[i].startswith("\\"):
        i += 1
    return runs, i


def parse_patch(text: str) -> List[Fragment]:
    lines = text.split("\n")
    fragments: List[Fragment] = []
    commit: Optional[CommitInfo] = None
    path: Optional[str] = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if COMMIT_LINE.match(line):
            commit, i = read_commit_header(lines, i)
            path = None
            continue
        if line.startswith("diff --git ") or line.startswith("Binary files "):
            path = None
        elif line.startswith("+++ "):
            target = unquote_path(line[4:])
            path = None if target == "/dev/null" else strip_side(target)
        else:
            hunk = HUNK_HEADER.match(line)
            if hunk and path is not None:
                old_count = 1 if hunk.group(1) is None else int(hunk.group(1))
                new_count = 1 if hunk.group(3) is None else int(hunk.group(3))
                runs, i = added_runs(lines, i + 1, old_count, int(hunk.group(2)), new_count)
                for start, added in runs:
                    fragments.append(
                        Fragment(
                            "\n".join(added) + "\n", path=path, start_line=start,
                            commit=commit.sha if commit else "", author=commit.author if commit else "",
                            email=commit.email if commit else "", date=commit.date if commit else "",
                            message=commit.message if commit else "",
                        )
                    )
                continue
        i += 1
    return fragments


def parse_messages(text: str) -> List[Fragment]:
    fragments = []
    for record in text.split("\x00"):
        record = record.lstrip("\n")
        if not record:
            continue
        parts = record.split("\x1f", 4)
        if len(parts) != 5 or not re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            raise InputError(f"unexpected git log record: {record[:80]!r}")
        sha, author, email, date, body = parts
        if body.strip():
            message = body.rstrip("\n")
            fragments.append(Fragment(message + "\n", path="", start_line=1, commit=sha, author=author, email=email, date=to_utc(date), message=message, where="message"))
    return fragments


Runner = Callable[[Sequence[str]], str]


def run_git(args: Sequence[str]) -> str:
    try:
        proc = subprocess.run(list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise InputError("git is not installed or not on PATH")
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip() or f"exit {proc.returncode}"
        raise InputError(f"{' '.join(args[:5])} ... failed: {detail}")
    return proc.stdout.decode("utf-8", errors="replace")


def git_base(repo: str) -> List[str]:
    return ["git", "-C", repo, "-c", "core.quotePath=false"]


def log_args(repo: str, log_opts: str = "") -> List[str]:
    selection = log_opts.split() if log_opts else GIT_LOG_FLAGS
    return git_base(repo) + ["log", "-p", "-U0"] + selection + GIT_FORMAT_FLAGS


def message_args(repo: str, log_opts: str = "") -> List[str]:
    selection = log_opts.split() if log_opts else ["--all"]
    return git_base(repo) + ["log", "-z", "--date=iso-strict", MESSAGE_FORMAT] + selection


def staged_args(repo: str) -> List[str]:
    return git_base(repo) + ["diff", "-U0", "--no-color", "--no-ext-diff", "--staged", "."]


def git_fragments(repo: str, runner: Runner = run_git, log_opts: str = "", messages: bool = True) -> List[Fragment]:
    fragments = parse_patch(runner(log_args(repo, log_opts)))
    if messages:
        fragments += parse_messages(runner(message_args(repo, log_opts)))
    return fragments


def staged_fragments(repo: str, runner: Runner = run_git) -> List[Fragment]:
    return parse_patch(runner(staged_args(repo)))


def filter_repo_expressions(findings: Iterable[Finding]) -> List[str]:
    lines: List[str] = []
    for finding in findings:
        replacement = f"REMOVED-{finding.rule}"
        if "\n" in finding.secret:
            expression = "regex:" + "\\n".join(re.escape(part) for part in finding.secret.split("\n"))
        else:
            expression = "literal:" + finding.secret
        line = f"{expression}==>{replacement}"
        if line not in lines:
            lines.append(line)
    return lines


def write_private(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def finding_json(finding: Finding, config: Config) -> Dict[str, Any]:
    shown = redact(finding.secret)
    rotate = ""
    try:
        rotate = config.rule(finding.rule).rotate
    except KeyError:
        pass
    return {
        "rule": finding.rule,
        "description": finding.description,
        "where": finding.where,
        "file": finding.file,
        "line": finding.line,
        "end_line": finding.end_line,
        "column": finding.column,
        "end_column": finding.end_column,
        "commit": finding.commit,
        "author": finding.author,
        "email": finding.email,
        "date": finding.date,
        "match": finding.match.replace(finding.secret, shown).split("\n", 1)[0],
        "secret": shown,
        "secret_sha256": finding.secret_sha256,
        "entropy": finding.entropy,
        "fingerprint": finding.fingerprint,
        "rotate": rotate,
    }


def remediation(findings: List[Finding], config: Config) -> Dict[str, Any]:
    dates = sorted(f.date for f in findings if f.date)
    rules: Dict[str, str] = {}
    for finding in findings:
        try:
            rules.setdefault(finding.rule, config.rule(finding.rule).rotate)
        except KeyError:
            rules.setdefault(finding.rule, "")
    return {"order": REMEDIATION_ORDER, "since": dates[0] if dates else "", "rules": rules}


def report_json(source: str, target: str, findings: List[Finding], stats: Stats, config: Config) -> Dict[str, Any]:
    return {
        "tool": "minileaks",
        "source": source,
        "target": target,
        "findings": [finding_json(f, config) for f in findings],
        "suppressed": dict(stats.suppressed),
        "scanned": {
            "fragments": stats.fragments,
            "bytes": stats.bytes,
            "commits": len(stats.commits),
            "files": len(stats.files),
            "rule_runs": stats.rule_runs,
            "prefiltered": stats.prefiltered,
            "paths_skipped": stats.paths_skipped,
        },
        "remediation": remediation(findings, config),
    }


def sarif_location(finding: Finding) -> Dict[str, Any]:
    if finding.where == "message":
        return {"logicalLocations": [{"name": finding.commit, "kind": "commit", "fullyQualifiedName": f"commit {finding.commit} message line {finding.line}"}]}
    region = {
        "startLine": finding.line,
        "startColumn": finding.column,
        "endLine": finding.end_line,
        "endColumn": finding.end_column + 1,
        "snippet": {"text": finding.match.replace(finding.secret, redact(finding.secret)).split("\n", 1)[0]},
    }
    return {"physicalLocation": {"artifactLocation": {"uri": finding.file}, "region": region}}


def report_sarif(findings: List[Finding], config: Config) -> Dict[str, Any]:
    used = []
    for finding in findings:
        if finding.rule not in used:
            used.append(finding.rule)
    rules = []
    for rule_id in used:
        try:
            rule = config.rule(rule_id)
            rules.append({"id": rule.id, "shortDescription": {"text": rule.description}, "help": {"text": rule.rotate}})
        except KeyError:
            rules.append({"id": rule_id, "shortDescription": {"text": rule_id}})
    results = []
    for finding in findings:
        where = f"commit {finding.commit[:7]}" if finding.commit else "the working tree"
        place = "the commit message" if finding.where == "message" else finding.file
        results.append(
            {
                "ruleId": finding.rule,
                "level": "error",
                "message": {"text": f"{finding.rule}: {finding.description} in {place} ({where})"},
                "locations": [sarif_location(finding)],
                "partialFingerprints": {
                    "commitSha": finding.commit,
                    "author": finding.author,
                    "email": finding.email,
                    "date": finding.date,
                    "secretSha256": finding.secret_sha256,
                },
                "fingerprints": {"minileaks/v1": finding.fingerprint},
            }
        )
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "minileaks", "informationUri": "https://cloudcaptain.io/learn/security/secret-scanning", "rules": rules}}, "results": results}],
    }


def render_text(source: str, target: str, findings: List[Finding], stats: Stats, config: Config) -> str:
    lines = [f"minileaks {source} {target}: {len(findings)} finding(s) in {stats.fragments} fragment(s), {len(stats.files)} file(s), {len(stats.commits)} commit(s)"]
    for finding in findings:
        place = f"{finding.file}:{finding.line}" if finding.where == "file" else f"(commit message):{finding.line}"
        commit = f"commit {finding.commit[:7]}" if finding.commit else "working tree"
        lines.append(f"  {finding.rule:<24} {place:<34} {commit:<15} {redact(finding.secret):<10} entropy {finding.entropy:.2f}")
    suppressed = ", ".join(f"{reason} {stats.suppressed[reason]}" for reason in SUPPRESSION_REASONS)
    total_runs = stats.rule_runs + stats.prefiltered
    lines.append(f"suppressed: {suppressed}; keyword prefilter skipped {stats.prefiltered} of {total_runs} rule runs; {stats.paths_skipped} path(s) allowlisted")
    if findings:
        plan = remediation(findings, config)
        lines.append("rotate first, then rewrite history:")
        lines += [f"  {n}. {step}" for n, step in enumerate(plan["order"], 1)]
        if plan["since"]:
            lines.append(f"  earliest commit date: {plan['since']}")
        grouped: Dict[str, List[str]] = {}
        for rule, text in plan["rules"].items():
            grouped.setdefault(text, []).append(rule)
        lines += [f"  {', '.join(rules)}: {text}" for text, rules in grouped.items()]
    return "\n".join(lines)


def toml_literal(text: str) -> str:
    if "'''" in text:
        raise InputError(f"cannot export {text!r} as a TOML literal string: it contains three single quotes")
    return "'''" + text + "'''"


def toml_allowlist(header: str, allowlist: Allowlist) -> List[str]:
    lines = [header]
    if allowlist.description:
        lines.append(f"description = {json.dumps(allowlist.description)}")
    if allowlist.regex_target != "secret":
        lines.append(f"regexTarget = {json.dumps(allowlist.regex_target)}")
    for key, values in (("paths", allowlist.paths), ("regexes", allowlist.regexes)):
        if values:
            lines.append(f"{key} = [" + ", ".join(toml_literal(v) for v in values) + "]")
    if allowlist.stopwords:
        lines.append("stopwords = [" + ", ".join(json.dumps(w) for w in allowlist.stopwords) + "]")
    if allowlist.commits:
        lines.append("commits = [" + ", ".join(json.dumps(c) for c in allowlist.commits) + "]")
    return lines


def gitleaks_toml(config: Config) -> str:
    lines = ['title = "minileaks rules exported for gitleaks"', ""]
    for allowlist in config.allowlists:
        lines += toml_allowlist("[[allowlists]]", allowlist) + [""]
    for rule in config.rules:
        lines += ["[[rules]]", f"id = {json.dumps(rule.id)}", f"description = {json.dumps(rule.description)}", f"regex = {toml_literal(rule.regex)}"]
        if rule.secret_group:
            lines.append(f"secretGroup = {rule.secret_group}")
        if rule.entropy:
            lines.append(f"entropy = {rule.entropy}")
        if rule.keywords:
            lines.append("keywords = [" + ", ".join(json.dumps(k) for k in rule.keywords) + "]")
        for allowlist in rule.allowlists:
            lines += toml_allowlist("[[rules.allowlists]]", allowlist)
        lines.append("")
    return "\n".join(lines)


def config_json(config: Config) -> Dict[str, Any]:
    def allow(a: Allowlist) -> Dict[str, Any]:
        body: Dict[str, Any] = {"description": a.description}
        for key in ("paths", "regexes", "stopwords", "commits"):
            if getattr(a, key):
                body[key] = getattr(a, key)
        if a.regex_target != "secret":
            body["regex_target"] = a.regex_target
        return body

    rules = []
    for r in config.rules:
        body: Dict[str, Any] = {"id": r.id, "description": r.description, "regex": r.regex, "keywords": r.keywords}
        if r.entropy:
            body["entropy"] = r.entropy
        if r.secret_group:
            body["secret_group"] = r.secret_group
        if r.validate:
            body["validate"] = r.validate
        if r.allowlists:
            body["allowlists"] = [allow(a) for a in r.allowlists]
        body["rotate"] = r.rotate
        rules.append(body)
    return {"use_default": False, "rules": rules, "allowlists": [allow(a) for a in config.allowlists]}


def render_rules(config: Config) -> str:
    lines = [f"{'rule':<24} {'entropy':>7}  keywords"]
    for rule in config.rules:
        lines.append(f"{rule.id:<24} {rule.entropy or '-':>7}  {', '.join(rule.keywords) or '(none: runs on every fragment)'}{'  validator ' + rule.validate if rule.validate else ''}")
    for allowlist in config.allowlists:
        lines.append(f"global allowlist: {allowlist.description or '(unnamed)'}: {len(allowlist.paths)} path(s), {len(allowlist.regexes)} regex(es), {len(allowlist.stopwords)} stopword(s)")
    return "\n".join(lines)


def hook_script(repo_main: str, python: str) -> str:
    return "#!/bin/sh\n" + f'{HOOK_MARKER} exec {shlex.quote(python)} {shlex.quote(repo_main)} staged "$(git rev-parse --show-toplevel)"\n'


def install_hook(repo: str, runner: Runner = run_git, force: bool = False, python: str = sys.executable) -> str:
    hooks = runner(git_base(repo) + ["rev-parse", "--git-path", "hooks"]).strip()
    if not os.path.isabs(hooks):
        hooks = os.path.join(repo, hooks)
    os.makedirs(hooks, exist_ok=True)
    path = os.path.join(hooks, "pre-commit")
    if os.path.exists(path) and not force:
        with open(path, encoding="utf-8", errors="replace") as handle:
            if HOOK_MARKER not in handle.read():
                raise InputError(f"{path} exists and was not written by minileaks; chain it by hand or pass --force")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(hook_script(os.path.abspath(__file__), python))
    os.chmod(path, 0o755)
    return path


def add_scan_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="JSON rules and allowlists (see `rules --format json`); merged with the defaults unless use_default is false")
    parser.add_argument("--baseline", help="a previous minileaks --json report or gitleaks JSON report; its findings no longer fail the scan")
    parser.add_argument("--format", choices=("text", "json", "sarif"), default="text")
    parser.add_argument("--json", action="store_const", const="json", dest="format", help="same as --format json")
    parser.add_argument("--sarif", action="store_const", const="sarif", dest="format", help="same as --format sarif")
    parser.add_argument("--report-path", help="write the report here instead of stdout and print a one-line summary")
    parser.add_argument("--no-inline-ignore", action="store_true", help="report lines marked gitleaks:allow or minileaks:allow")
    parser.add_argument("--replace-text", metavar="FILE", help="write git filter-repo expressions for every finding (contains the secrets; mode 0600)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minileaks", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    directory = sub.add_parser("dir", help="scan the files under a directory (or one file)")
    directory.add_argument("path", nargs="?", default=".")
    add_scan_options(directory)
    git = sub.add_parser("git", help="scan every commit reachable from any ref: added lines and commit messages")
    git.add_argument("repo", nargs="?", default=".")
    git.add_argument("--log-opts", default="", help="git log selection instead of --full-history --all, e.g. origin/main..HEAD")
    git.add_argument("--no-messages", action="store_true", help="skip commit messages, as gitleaks does")
    add_scan_options(git)
    staged = sub.add_parser("staged", help="pre-commit mode: scan only what is staged (git diff --staged)")
    staged.add_argument("repo", nargs="?", default=".")
    add_scan_options(staged)
    rules = sub.add_parser("rules", help="print the active rules, as a table, JSON config or gitleaks TOML")
    rules.add_argument("--config")
    rules.add_argument("--format", choices=("table", "json", "gitleaks"), default="table")
    entropy = sub.add_parser("entropy", help="print the Shannon entropy of each argument, in bits per character")
    entropy.add_argument("values", nargs="+")
    hook = sub.add_parser("install-hook", help="install a Git pre-commit hook that runs `minileaks staged`")
    hook.add_argument("repo", nargs="?", default=".")
    hook.add_argument("--force", action="store_true", help="replace an existing pre-commit hook")
    return parser


def emit(args: argparse.Namespace, source: str, target: str, findings: List[Finding], stats: Stats, config: Config) -> None:
    if args.format == "json":
        output = json.dumps(report_json(source, target, findings, stats, config), indent=2)
    elif args.format == "sarif":
        output = json.dumps(report_sarif(findings, config), indent=2)
    else:
        output = render_text(source, target, findings, stats, config)
    if args.report_path:
        with open(args.report_path, "w", encoding="utf-8") as handle:
            handle.write(output + "\n")
        print(f"minileaks {source} {target}: {len(findings)} finding(s); {args.format} report written to {args.report_path}")
    else:
        print(output)
    if args.replace_text:
        write_private(args.replace_text, "\n".join(filter_repo_expressions(findings)) + ("\n" if findings else ""))


def main(argv: Optional[Sequence[str]] = None, runner: Runner = run_git) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "entropy":
            for value in args.values:
                print(f"{shannon_entropy(value):.3f}  {len(value):>4} chars  {redact(value) if len(value) > 8 else value}")
            return 0
        if args.command == "install-hook":
            print(f"installed {install_hook(args.repo, runner, args.force)}")
            return 0
        config = load_config(args.config)
        if args.command == "rules":
            if args.format == "gitleaks":
                print(gitleaks_toml(config))
            elif args.format == "json":
                print(json.dumps(config_json(config), indent=2))
            else:
                print(render_rules(config))
            return 0
        baseline = load_baseline(args.baseline) if args.baseline else None
        stats = Stats()
        if args.command == "dir":
            fragments: Iterable[Fragment] = dir_fragments(args.path, config, stats)
            target = args.path
        elif args.command == "git":
            fragments = git_fragments(args.repo, runner, args.log_opts, messages=not args.no_messages)
            target = args.repo
        else:
            fragments = staged_fragments(args.repo, runner)
            target = args.repo
        findings, stats = scan(fragments, config, baseline, not args.no_inline_ignore, stats)
        emit(args, args.command, target, findings, stats, config)
    except InputError as exc:
        print(f"minileaks: {exc}", file=sys.stderr)
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
