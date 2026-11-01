"""minirelease: Conventional Commits to the next SemVer version, a Keep a Changelog section and an annotated tag, plus a dependency-update check."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

Runner = Callable[[Sequence[str]], Tuple[int, str, str]]

SECTIONS = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
SECTION_RULES: Tuple[Tuple[str, str, Optional[str]], ...] = (
    ("type", r"(build|chore|ci|docs|style|test)$", None),
    ("scope", r"security$", "Security"),
    ("description", r"(?i:deprecate)", "Deprecated"),
    ("description", r"(?i:remove|drop)", "Removed"),
    ("type", r"feat$", "Added"),
    ("type", r"fix$", "Fixed"),
    ("type", r"(perf|refactor|revert)$", "Changed"),
)
BREAKING_TOKENS = ("BREAKING CHANGE", "BREAKING-CHANGE")
HEADER = re.compile(r"(?P<type>[A-Za-z]+)(?:\((?P<scope>[^()\r\n]+)\))?(?P<bang>!)?: (?P<description>\S.*)")
FOOTER = re.compile(r"(?P<token>BREAKING CHANGE|BREAKING-CHANGE|[A-Za-z][A-Za-z0-9-]*)(?P<separator>: | #)(?P<value>.*)")
IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    rf"(?:-({IDENTIFIER}(?:\.{IDENTIFIER})*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)
CHANNEL = re.compile(r"[A-Za-z][0-9A-Za-z-]*")
LOG_FORMAT = "--format=%H%x1f%an%x1f%cI%x1f%B%x1e"
TAG_FORMAT = "--format=%(refname) %(objecttype) %(objectname) %(*objectname)"
CHANGELOG_HEADER = (
    "# Changelog\n\n"
    "All notable changes to this project will be documented in this file.\n\n"
    "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),\n"
    "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)."
)


class ReleaseError(Exception):
    pass


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    pre: Tuple[str, ...] = ()
    build: Tuple[str, ...] = ()

    @property
    def core(self) -> Tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def precedence(self) -> Tuple[Any, ...]:
        return (self.core, prerelease_key(self.pre))

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.pre:
            text += "-" + ".".join(self.pre)
        if self.build:
            text += "+" + ".".join(self.build)
        return text


def prerelease_key(pre: Sequence[str]) -> Tuple[Any, ...]:
    if not pre:
        return (1, ())
    return (0, tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre))


def parse_semver(text: str) -> SemVer:
    match = SEMVER.fullmatch(text.strip())
    if not match:
        raise ReleaseError(f"not a SemVer version: {text!r} (want MAJOR.MINOR.PATCH such as 1.4.0 or 2.0.0-rc.1)")
    major, minor, patch, pre, build = match.groups()
    return SemVer(int(major), int(minor), int(patch), tuple(pre.split(".")) if pre else (), tuple(build.split(".")) if build else ())


def increment(version: SemVer, level: str) -> SemVer:
    if level == "major":
        return SemVer(version.major + 1, 0, 0)
    if level == "minor":
        return SemVer(version.major, version.minor + 1, 0)
    return SemVer(version.major, version.minor, version.patch + 1)


@dataclass
class Footer:
    token: str
    separator: str
    value: str
    breaking: bool = False


@dataclass
class ConventionalCommit:
    type: str
    scope: str
    description: str
    body: str
    footers: List[Footer]
    breaking: bool
    breaking_description: str


@dataclass
class Commit:
    sha: str
    author: str
    date: str
    subject: str
    message: str
    conventional: Optional[ConventionalCommit]
    problem: str
    section: Optional[str]


def header_problem(subject: str) -> str:
    if re.match(r"(fixup|squash|amend)! ", subject):
        return "an autosquash commit: squash it before the release"
    if subject.startswith("Merge "):
        return "a merge commit"
    loose = re.match(r"([A-Za-z]+)(\([^)]*\))?!?:(.*)", subject)
    if not loose:
        return "no `type: description` header"
    if loose.group(2) == "()":
        return "an empty scope"
    if not loose.group(3).startswith(" "):
        return "the colon must be followed by a space"
    return "an empty description"


def split_footers(lines: Sequence[str]) -> Tuple[str, List[Footer]]:
    start = None
    for index, line in enumerate(lines):
        starts_paragraph = index == 0 or not lines[index - 1].strip()
        if starts_paragraph and FOOTER.fullmatch(line.rstrip()):
            start = index
            break
    if start is None:
        return "\n".join(lines).strip(), []
    footers: List[Footer] = []
    for line in lines[start:]:
        match = FOOTER.fullmatch(line.rstrip())
        if match:
            token = match.group("token")
            footers.append(Footer(token, match.group("separator"), match.group("value"), token in BREAKING_TOKENS))
        else:
            footers[-1].value += "\n" + line
    for footer in footers:
        footer.value = footer.value.strip()
    return "\n".join(lines[:start]).strip(), footers


def parse_message(message: str) -> Tuple[Optional[ConventionalCommit], str]:
    lines = message.replace("\r\n", "\n").strip("\n").split("\n")
    subject = lines[0].strip()
    match = HEADER.fullmatch(subject)
    if not match:
        return None, header_problem(subject)
    rest = lines[1:]
    if rest and rest[0].strip():
        return None, "the body must start after one blank line"
    body, footers = split_footers(rest[1:])
    breaking_footers = [f for f in footers if f.breaking]
    bang = bool(match.group("bang"))
    description = match.group("description").strip()
    return ConventionalCommit(
        type=match.group("type").lower(),
        scope=match.group("scope") or "",
        description=description,
        body=body,
        footers=footers,
        breaking=bang or bool(breaking_footers),
        breaking_description=breaking_footers[0].value if breaking_footers else (description if bang else ""),
    ), ""


def section_for(cc: Optional[ConventionalCommit]) -> Optional[str]:
    if cc is None:
        return None
    for field_name, pattern, section in SECTION_RULES:
        if not re.match(pattern, getattr(cc, field_name)):
            continue
        if section is not None:
            return section
        if not cc.breaking:
            return None
    return "Changed" if cc.breaking else None


def make_commit(sha: str, author: str, date: str, message: str) -> Commit:
    cc, problem = parse_message(message)
    subject = message.strip("\n").split("\n")[0].strip()
    return Commit(sha, author, date, subject, message.strip("\n"), cc, problem, section_for(cc))


def skip_reason(commit: Commit) -> str:
    if commit.conventional is None:
        return f"not conventional: {commit.problem}"
    return f"hidden type `{commit.conventional.type}`" if commit.section is None else ""


@dataclass
class Policy:
    breaking_always_bump_major: bool = False
    features_always_bump_minor: bool = False
    initial: str = "0.1.0"


def bump_level(commits: Sequence[Commit], current: SemVer, policy: Policy) -> Tuple[Optional[str], str]:
    visible = [c for c in commits if c.section and c.conventional]
    if not visible:
        return None, "no commit since the last release would appear in the changelog"
    breaking = any(c.conventional.breaking for c in visible)
    feature = any(c.conventional.type == "feat" for c in visible)
    zero = current.major == 0
    if breaking and (not zero or policy.breaking_always_bump_major):
        return "major", "a breaking change bumps the major version"
    if feature and (not zero or policy.features_always_bump_minor):
        return "minor", "a new feature bumps the minor version"
    if breaking and current.minor > 0:
        return "minor", "a breaking change in 0.y.z bumps the minor version (the 0.x rule)"
    if breaking:
        return "patch", "a breaking change in 0.0.z bumps the patch version (the 0.x rule)"
    if feature:
        return "patch", "a new feature in 0.y.z bumps the patch version (the 0.x rule)"
    return "patch", "fixes and other visible changes bump the patch version"


@dataclass
class Tag:
    name: str
    version: SemVer
    commit: str
    annotated: bool


@dataclass
class Release:
    version: str
    tag: str
    date: str
    commits: List[Commit]


@dataclass
class History:
    tags: List[Tag]
    releases: List[Release]
    since_stable: List[Commit]

    @property
    def unreleased(self) -> Release:
        return self.releases[0]


@dataclass
class Decision:
    version: Optional[SemVer]
    level: Optional[str]
    reason: str


def decide(history: History, policy: Policy, channel: str = "") -> Decision:
    if channel and not CHANNEL.fullmatch(channel):
        raise ReleaseError(f"pre-release channel {channel!r} must be letters, digits and dashes, starting with a letter (alpha, beta, rc)")
    tags = history.tags
    latest = tags[-1].version if tags else None
    stable = [t.version for t in tags if not t.version.pre]
    promoting = latest is not None and bool(latest.pre) and not channel
    if not promoting and not any(c.section for c in history.unreleased.commits):
        since = f"since {tags[-1].name}" if tags else "in the history"
        return Decision(None, None, f"nothing to release: no commit {since} would appear in the changelog")
    if stable:
        level, reason = bump_level(history.since_stable, stable[-1], policy)
        if level is None:
            return Decision(None, None, f"nothing to release: {reason}")
        target = increment(stable[-1], level)
    elif latest is not None:
        level, reason = "pre-release", f"no stable release yet: continue the {SemVer(*latest.core)} pre-releases"
        target = SemVer(*latest.core)
    else:
        level, reason = "initial", f"no release tag yet: start at {policy.initial}"
        target = parse_semver(policy.initial)
    if channel:
        numbers = [int(t.version.pre[1]) for t in tags if t.version.core == target.core and len(t.version.pre) == 2 and t.version.pre[0] == channel and t.version.pre[1].isdigit()]
        target = SemVer(*target.core, pre=(channel, str(max(numbers, default=0) + 1)))
        reason += f"; pre-release number {target.pre[1]} on the {channel} channel"
    if latest is not None and target.precedence() <= latest.precedence():
        raise ReleaseError(f"the next version {target} would not be greater than the latest tag {tags[-1].name}; pick a channel that sorts after {'.'.join(latest.pre) or 'it'} or release without --pre")
    return Decision(target, level, reason)


def subprocess_runner(argv: Sequence[str]) -> Tuple[int, str, str]:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: command not found"
    return done.returncode, done.stdout, done.stderr


def git(runner: Runner, repo: str, *args: str) -> str:
    code, out, err = runner(["git", "-C", repo, *args])
    if code != 0:
        raise ReleaseError(f"`git {' '.join(args[:3])}` failed (exit {code}): {(err or out).strip()}")
    return out


def parse_tags(text: str, prefix: str) -> List[Tag]:
    tags = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name = parts[0][len("refs/tags/"):] if parts[0].startswith("refs/tags/") else parts[0]
        if not name.startswith(prefix):
            continue
        try:
            version = parse_semver(name[len(prefix):])
        except ReleaseError:
            continue
        annotated = parts[1] == "tag"
        tags.append(Tag(name, version, parts[3] if annotated and len(parts) > 3 else parts[2], annotated))
    return sorted(tags, key=lambda t: t.version.precedence())


def parse_log(text: str) -> List[Commit]:
    commits = []
    for record in text.split("\x1e"):
        record = record.lstrip("\n")
        if not record.strip():
            continue
        sha, author, date, message = (record.split("\x1f", 3) + ["", "", ""])[:4]
        commits.append(make_commit(sha, author, date, message))
    return list(reversed(commits))


def utc_day(stamp: str) -> str:
    try:
        return dt.datetime.fromisoformat(stamp).astimezone(dt.timezone.utc).date().isoformat()
    except ValueError:
        return stamp[:10]


def load_history(repo: str, prefix: str, runner: Runner) -> History:
    tags = parse_tags(git(runner, repo, "for-each-ref", "--merged", "HEAD", TAG_FORMAT, "refs/tags"), prefix)
    releases: List[Release] = []
    previous = ""
    for tag in tags:
        rev = f"refs/tags/{tag.name}"
        commits = parse_log(git(runner, repo, "log", LOG_FORMAT, f"{previous}..{rev}" if previous else rev))
        date = next((utc_day(c.date) for c in commits if c.sha == tag.commit), "")
        releases.insert(0, Release(str(tag.version), tag.name, date, commits))
        previous = rev
    unreleased = parse_log(git(runner, repo, "log", LOG_FORMAT, f"{previous}..HEAD" if previous else "HEAD"))
    releases.insert(0, Release("", "", "", unreleased))
    stable = [t for t in tags if not t.version.pre]
    if not stable:
        since_stable = [c for r in reversed(releases) for c in r.commits]
    elif stable[-1] is tags[-1]:
        since_stable = unreleased
    else:
        since_stable = parse_log(git(runner, repo, "log", LOG_FORMAT, f"refs/tags/{stable[-1].name}..HEAD"))
    return History(tags, releases, since_stable)


def entry(commit: Commit) -> str:
    cc = commit.conventional
    breaking = "**BREAKING** " if cc.breaking else ""
    scope = f"**{cc.scope}:** " if cc.scope else ""
    return f"- {breaking}{scope}{cc.description}"


def grouped(release: Release) -> Dict[str, List[str]]:
    return {s: [entry(c) for c in release.commits if c.section == s] for s in SECTIONS if any(c.section == s for c in release.commits)}


def render_release(release: Release) -> List[str]:
    lines = [f"## [{release.version}] - {release.date}" if release.version else "## [Unreleased]"]
    for section, entries in grouped(release).items():
        lines += ["", f"### {section}", ""] + entries
    return lines


def links(releases: Sequence[Release], url: str) -> List[str]:
    out = []
    for index, release in enumerate(releases):
        older = next((r for r in releases[index + 1:] if r.tag), None)
        label = release.version or "Unreleased"
        if older:
            out.append(f"[{label}]: {url}/compare/{older.tag}...{release.tag or 'HEAD'}")
        elif release.tag:
            out.append(f"[{label}]: {url}/releases/tag/{release.tag}")
    return out


def render_changelog(releases: Sequence[Release], url: str = "") -> str:
    lines = [CHANGELOG_HEADER]
    for release in releases:
        lines += [""] + render_release(release)
    footer = links(releases, url.rstrip("/")) if url else []
    if footer:
        lines += [""] + footer
    return "\n".join(lines) + "\n"


def with_next(history: History, decision: Decision, prefix: str, today: str) -> List[Release]:
    if decision.version is None:
        return list(history.releases)
    current = history.unreleased
    return [Release(str(decision.version), prefix + str(decision.version), today, current.commits)] + list(history.releases[1:])


def tag_message(release: Release, previous: str) -> str:
    lines = [f"Release {release.tag}", ""]
    sections = render_release(release)[2:]
    lines += sections if sections else [f"Promotes {previous} with no further changes." if previous else "No user-visible changes."]
    notes = [c for c in release.commits if c.section and c.conventional.breaking]
    if notes:
        lines += ["", "Breaking changes:"] + [f"- {c.conventional.breaking_description}" for c in notes]
    return "\n".join(lines) + "\n"


def commit_json(commit: Commit) -> Dict[str, Any]:
    cc = commit.conventional
    data: Dict[str, Any] = {"sha": commit.sha, "subject": commit.subject, "section": commit.section}
    if cc is None:
        data.update({"conventional": False, "problem": commit.problem})
    else:
        data.update({"conventional": True, "type": cc.type, "scope": cc.scope, "description": cc.description, "breaking": cc.breaking, "breaking_description": cc.breaking_description, "footers": [asdict(f) for f in cc.footers]})
    return data


def release_json(release: Release) -> Dict[str, Any]:
    return {
        "version": release.version or None,
        "tag": release.tag or None,
        "date": release.date or None,
        "sections": grouped(release),
        "skipped": [{"sha": c.sha, "subject": c.subject, "why": skip_reason(c)} for c in release.commits if not c.section],
    }


def decision_json(history: History, decision: Decision, prefix: str) -> Dict[str, Any]:
    current = history.tags[-1].name if history.tags else None
    return {
        "current": current,
        "next": prefix + str(decision.version) if decision.version else None,
        "version": str(decision.version) if decision.version else None,
        "level": decision.level,
        "reason": decision.reason,
        "commits": [commit_json(c) for c in history.unreleased.commits],
    }


def render_log(history: History) -> str:
    since = history.tags[-1].name if history.tags else "the first commit"
    lines = [f"{len(history.unreleased.commits)} commit(s) since {since}"]
    for commit in history.unreleased.commits:
        flag = "!" if commit.conventional and commit.conventional.breaking else " "
        where = commit.section or skip_reason(commit)
        lines.append(f"  {commit.sha[:7]} {flag} {commit.subject[:60]:<60}  {where}")
    return "\n".join(lines)


def run_release(args: argparse.Namespace, history: History, decision: Decision, runner: Runner, today: str) -> int:
    releases = with_next(history, decision, args.tag_prefix, today)
    release = releases[0]
    previous = history.tags[-1].name if history.tags else ""
    message = tag_message(release, previous)
    print(f"minirelease: {previous or '(no tag)'} -> {release.tag} ({decision.reason})")
    if args.dry_run:
        print(message, end="")
        return 0
    if args.changelog:
        path = os.path.join(args.repo, args.changelog)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(render_changelog(releases, args.compare_url))
        print(f"  wrote {args.changelog}")
        if args.commit:
            git(runner, args.repo, "add", "--", args.changelog)
            git(runner, args.repo, "commit", "--quiet", "-m", f"chore(release): {release.tag}")
            print(f"  committed chore(release): {release.tag}")
    git(runner, args.repo, "tag", "-s" if args.sign else "-a", "--cleanup=whitespace", "-m", message, release.tag)
    print(f"  created {'signed' if args.sign else 'annotated'} tag {release.tag}; publish it with: git push origin {release.tag}")
    return 0


@dataclass(frozen=True)
class Version:
    text: str
    release: Tuple[int, ...]
    key: Tuple[Any, ...]
    pre: bool = False
    post: bool = False
    suffix: str = ""


PEP440 = re.compile(
    r"v?(?:(?P<epoch>[0-9]+)!)?(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?:[-_.]?(?P<pre_l>alpha|beta|preview|pre|rc|a|b|c)[-_.]?(?P<pre_n>[0-9]*))?"
    r"(?:-(?P<post_n1>[0-9]+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]*))?"
    r"(?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]*))?"
)
PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}
NPM_PARTIAL = re.compile(r"v?(?P<major>[0-9]+|[xX*])(?:\.(?P<minor>[0-9]+|[xX*]))?(?:\.(?P<patch>[0-9]+|[xX*]))?(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")
PIP_SPEC = re.compile(r"\s*(~=|===|==|!=|<=|>=|<|>)\s*([^\s,;]+)\s*")
ACTION_REF = re.compile(r"v?([0-9]+)(?:\.([0-9]+))?(?:\.([0-9]+))?(?:-([0-9A-Za-z.-]+))?")
SHA = re.compile(r"[0-9a-f]{40}")
FROM = re.compile(r"\s*FROM\s+(?:--platform=\S+\s+)?(?P<image>\S+)(?:\s+AS\s+(?P<stage>\S+))?\s*", re.IGNORECASE)
USES = re.compile(r"\s*(?:-\s+)?uses:\s*['\"]?(?P<ref>[^'\"\s#]+)['\"]?\s*(?:#\s*(?P<comment>\S+).*)?")
REQUIREMENT = re.compile(r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<extras>\[[^\]]*\])?\s*(?P<spec>[^;@]*)")
NPM_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")


def pad(release: Sequence[int], size: int) -> Tuple[int, ...]:
    return tuple(release) + (0,) * (size - len(release))


def pep440(text: str) -> Optional[Version]:
    match = PEP440.fullmatch(text.strip().lower())
    if not match:
        return None
    g = match.groupdict()
    release = tuple(int(p) for p in g["release"].split("."))
    trimmed = release
    while len(trimmed) > 1 and trimmed[-1] == 0:
        trimmed = trimmed[:-1]
    has_post = g["post_n1"] is not None or g["post_l"] is not None
    has_dev = g["dev_l"] is not None
    if g["pre_l"]:
        pre_key: Tuple[int, ...] = (0, PRE_RANK[g["pre_l"]], int(g["pre_n"] or 0))
    elif has_dev and not has_post:
        pre_key = (-1, 0, 0)
    else:
        pre_key = (1, 0, 0)
    post_key = int(g["post_n1"] or g["post_n2"] or 0) if has_post else -1
    dev_key = (0, int(g["dev_n"] or 0)) if has_dev else (1, 0)
    key = (int(g["epoch"] or 0), trimmed, pre_key, post_key, dev_key)
    return Version(text, release, key, bool(g["pre_l"]) or has_dev, has_post)


def npm_semver(text: str) -> Optional[SemVer]:
    try:
        return parse_semver(text.strip().lstrip("=v"))
    except ReleaseError:
        return None


def npm_version(text: str) -> Optional[Version]:
    version = npm_semver(text)
    if version is None:
        return None
    return Version(text, version.core, version.precedence(), bool(version.pre))


def docker_tag(text: str) -> Optional[Version]:
    match = re.fullmatch(r"v?([0-9]+(?:\.[0-9]+)*)(.*)", text)
    if not match:
        return None
    release = tuple(int(p) for p in match.group(1).split("."))
    suffix = match.group(2)
    return Version(text, release, (pad(release, 4), len(release)), bool(suffix) and not suffix.startswith("-"), suffix=suffix)


def action_tag(text: str) -> Optional[Version]:
    match = ACTION_REF.fullmatch(text)
    if not match:
        return None
    release = tuple(int(p) for p in match.groups()[:3] if p is not None)
    pre = tuple(match.group(4).split(".")) if match.group(4) else ()
    return Version(text, release, (pad(release, 3), prerelease_key(pre)), bool(pre))


PARSERS: Dict[str, Callable[[str], Optional[Version]]] = {"pypi": pep440, "npm": npm_version, "docker": docker_tag, "github-tags": action_tag}


Comparator = Tuple[str, SemVer]


def partial(text: str) -> Tuple[Optional[int], Optional[int], Optional[int], Tuple[str, ...]]:
    match = NPM_PARTIAL.fullmatch(text)
    if not match:
        raise ReleaseError(f"not an npm version or range: {text!r}")
    numbers: List[Optional[int]] = []
    for name in ("major", "minor", "patch"):
        value = match.group(name)
        numbers.append(None if value is None or value in "xX*" else int(value))
    for index in (1, 2):
        if numbers[index - 1] is None:
            numbers[index] = None
    pre = tuple(match.group("pre").split(".")) if match.group("pre") and numbers[2] is not None else ()
    return numbers[0], numbers[1], numbers[2], pre


def desugar(op: str, text: str) -> List[Comparator]:
    major, minor, patch, pre = partial(text)
    zero = ("0",)
    if major is None:
        return [("<", SemVer(0, 0, 0, zero))] if op in ("<", ">") else []
    if op == "^":
        if minor is None:
            return [(">=", SemVer(major, 0, 0)), ("<", SemVer(major + 1, 0, 0, zero))]
        if patch is None:
            upper = SemVer(major + 1, 0, 0, zero) if major else SemVer(0, minor + 1, 0, zero)
            return [(">=", SemVer(major, minor, 0)), ("<", upper)]
        upper = SemVer(major + 1, 0, 0, zero) if major else SemVer(0, minor + 1, 0, zero) if minor else SemVer(0, 0, patch + 1, zero)
        return [(">=", SemVer(major, minor, patch, pre)), ("<", upper)]
    if op in ("~", "~>") or (op in ("", "=") and patch is None):
        if minor is None:
            return [(">=", SemVer(major, 0, 0)), ("<", SemVer(major + 1, 0, 0, zero))]
        return [(">=", SemVer(major, minor, patch or 0, pre)), ("<", SemVer(major, minor + 1, 0, zero))]
    if op in ("", "="):
        return [("=", SemVer(major, minor or 0, patch, pre))]
    if op == ">=":
        return [(">=", SemVer(major, minor or 0, patch or 0, pre))]
    if op == ">":
        if minor is None:
            return [(">=", SemVer(major + 1, 0, 0))]
        if patch is None:
            return [(">=", SemVer(major, minor + 1, 0))]
        return [(">", SemVer(major, minor, patch, pre))]
    if op == "<":
        if minor is None:
            return [("<", SemVer(major, 0, 0, zero))]
        return [("<", SemVer(major, minor, patch or 0, pre if patch is not None else zero))]
    if minor is None:
        return [("<", SemVer(major + 1, 0, 0, zero))]
    if patch is None:
        return [("<", SemVer(major, minor + 1, 0, zero))]
    return [("<=", SemVer(major, minor, patch, pre))]


def npm_range(text: str) -> List[List[Comparator]]:
    sets = []
    for part in text.split("||"):
        part = part.strip()
        hyphen = re.fullmatch(r"(\S+)\s+-\s+(\S+)", part)
        if hyphen:
            sets.append(desugar(">=", hyphen.group(1)) + desugar("<=", hyphen.group(2)))
            continue
        comparators: List[Comparator] = []
        for token in re.sub(r"(<=|>=|<|>|=|\^|~>?)\s+", r"\1", part).split():
            op, rest = re.fullmatch(r"(<=|>=|<|>|=|\^|~>|~|)(.*)", token).groups()
            comparators += desugar(op, rest)
        sets.append(comparators)
    return sets


def holds(op: str, version: SemVer, target: SemVer) -> bool:
    a, b = version.precedence(), target.precedence()
    return {"=": a == b, ">=": a >= b, ">": a > b, "<=": a <= b, "<": a < b}[op]


def npm_satisfies(version: SemVer, ranges: Sequence[Sequence[Comparator]]) -> bool:
    for comparators in ranges:
        if not all(holds(op, version, target) for op, target in comparators):
            continue
        if not version.pre or any(target.pre and target.core == version.core for _, target in comparators):
            return True
    return False


def pip_specifiers(text: str) -> List[Tuple[str, str]]:
    specs = []
    for item in [p for p in text.split(",") if p.strip()]:
        match = PIP_SPEC.fullmatch(item)
        if not match:
            raise ReleaseError(f"not a PEP 440 specifier: {item.strip()!r}")
        specs.append((match.group(1), match.group(2)))
    return specs


def same_release(a: Version, b: Version) -> bool:
    return a.key[:2] == b.key[:2]


def prefix_match(version: Version, spec: Version, prefix: Sequence[int]) -> bool:
    return version.key[0] == spec.key[0] and pad(version.release, len(prefix))[: len(prefix)] == tuple(prefix)


def pip_one(op: str, target: str, version: Version) -> bool:
    if op == "===":
        return version.text == target
    spec = pep440(target[:-2] if target.endswith(".*") else target)
    if spec is None:
        raise ReleaseError(f"not a PEP 440 version: {target!r}")
    if target.endswith(".*"):
        matched = prefix_match(version, spec, spec.release)
        return matched if op == "==" else not matched
    a, b = version.key, spec.key
    if op == "~=":
        return a >= b and prefix_match(version, spec, spec.release[:-1])
    if op == "<":
        return a < b and (spec.pre or not (version.pre and same_release(version, spec)))
    if op == ">":
        return a > b and (spec.post or not (version.post and same_release(version, spec)))
    return {"==": a == b, "!=": a != b, "<=": a <= b, ">=": a >= b}[op]


def implies_prerelease(op: str, target: str) -> bool:
    spec = pep440(target)
    return op != "!=" and not target.endswith(".*") and spec is not None and spec.pre


def pip_satisfies(version: Version, specs: Sequence[Tuple[str, str]]) -> bool:
    if version.pre and not any(implies_prerelease(op, t) for op, t in specs):
        return False
    return all(pip_one(op, t, version) for op, t in specs)


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class Dependency:
    manager: str
    file: str
    line: int
    name: str
    value: str
    current: str
    datasource: str
    style: str
    digest: str = ""
    note: str = ""
    skip: str = ""


@dataclass
class Update:
    manager: str
    file: str
    line: int
    name: str
    current_value: str
    new_value: str
    from_version: str
    to_version: str
    update_type: str
    breaking: bool
    group: str
    in_range: bool
    note: str


def manager_for(path: str) -> Optional[str]:
    rel = path.replace(os.sep, "/")
    base = rel.rsplit("/", 1)[-1]
    if re.fullmatch(r"requirements.*\.txt", base):
        return "pip"
    if base in ("Dockerfile", "Containerfile") or base.endswith(".Dockerfile") or base.startswith("Dockerfile."):
        return "dockerfile"
    if re.search(r"(^|/)\.github/workflows/[^/]+\.ya?ml$", rel) or base in ("action.yml", "action.yaml"):
        return "github-actions"
    if base == "package.json":
        return "npm"
    return None


def discover(root: str) -> List[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ("node_modules", ".git", ".venv", "venv", "__pycache__"))
        for name in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            if manager_for(rel):
                found.append(rel)
    return found


def extract_pip(path: str, text: str) -> List[Dependency]:
    deps = []
    logical: List[Tuple[int, str]] = []
    pending, start = "", 0
    for number, raw in enumerate(text.splitlines(), 1):
        if not pending:
            start = number
        line = raw.split(" #", 1)[0] if not raw.lstrip().startswith("#") else ""
        if line.rstrip().endswith("\\"):
            pending += line.rstrip()[:-1] + " "
            continue
        logical.append((start, pending + line))
        pending = ""
    for number, line in logical:
        content = line.strip()
        if not content or content.startswith("-"):
            continue
        hashed = "--hash=" in content
        requirement = content.split(" --", 1)[0].strip()
        match = REQUIREMENT.match(requirement)
        if not match:
            continue
        name = match.group("name")
        spec = match.group("spec").strip()
        if "@" in requirement or "://" in requirement:
            deps.append(Dependency("pip", path, number, name, requirement, "", "pypi", "url", skip="a direct URL reference"))
            continue
        note = "regenerate the --hash lines (pip-compile --generate-hashes)" if hashed else ""
        try:
            specs = pip_specifiers(spec)
        except ReleaseError as error:
            deps.append(Dependency("pip", path, number, name, spec, "", "pypi", "unknown", skip=str(error)))
            continue
        if not specs:
            deps.append(Dependency("pip", path, number, name, "", "", "pypi", "unpinned", note=note))
            continue
        floor = next((t for op, t in specs if op in ("==", "~=", ">=", "===") and not t.endswith(".*")), "")
        style = "exact" if len(specs) == 1 and specs[0][0] in ("==", "===") and not floor.endswith(".*") else "range"
        if not floor:
            deps.append(Dependency("pip", path, number, name, spec, "", "pypi", style, skip="no lower bound to compare with"))
            continue
        deps.append(Dependency("pip", path, number, name, spec.replace(" ", ""), floor, "pypi", style, note=note))
    return deps


def image_dependency(manager: str, path: str, number: int, image: str) -> Dependency:
    name_tag, _, digest = image.partition("@")
    slash = name_tag.rfind("/")
    colon = name_tag.rfind(":")
    name, tag = (name_tag[:colon], name_tag[colon + 1:]) if colon > slash else (name_tag, "")
    if "$" in image:
        return Dependency(manager, path, number, name, image, "", "docker", "arg", skip="the tag comes from a build argument")
    if not tag and digest:
        return Dependency(manager, path, number, name, image, "", "docker", "digest", digest, skip="pinned by digest only: add the tag next to it so updates can be proposed")
    if not tag or tag == "latest":
        return Dependency(manager, path, number, name, image, "", "docker", "unpinned", digest)
    style = "digest" if digest else "exact"
    note = "re-pin the digest of the new tag" if digest else ""
    return Dependency(manager, path, number, name, image, tag, "docker", style, digest, note)


def extract_dockerfile(path: str, text: str) -> List[Dependency]:
    deps = []
    stages: List[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        match = FROM.fullmatch(line)
        if not match:
            continue
        image, stage = match.group("image"), match.group("stage")
        if image.lower() in stages:
            deps.append(Dependency("dockerfile", path, number, image, image, "", "docker", "stage", skip="a reference to an earlier build stage"))
        elif image == "scratch":
            deps.append(Dependency("dockerfile", path, number, image, image, "", "docker", "scratch", skip="the empty base image"))
        else:
            deps.append(image_dependency("dockerfile", path, number, image))
        if stage:
            stages.append(stage.lower())
    return deps


def extract_actions(path: str, text: str) -> List[Dependency]:
    deps = []
    for number, line in enumerate(text.splitlines(), 1):
        match = USES.fullmatch(line)
        if not match:
            continue
        ref, comment = match.group("ref"), match.group("comment") or ""
        if ref.startswith("./") or ref.startswith("../"):
            deps.append(Dependency("github-actions", path, number, ref, ref, "", "github-tags", "local", skip="a local action in this repository"))
            continue
        if ref.startswith("docker://"):
            deps.append(image_dependency("github-actions", path, number, ref[len("docker://"):]))
            continue
        action, _, version = ref.partition("@")
        name = "/".join(action.split("/")[:2])
        if SHA.fullmatch(version):
            if action_tag(comment):
                deps.append(Dependency("github-actions", path, number, name, ref, comment, "github-tags", "digest", version))
            else:
                deps.append(Dependency("github-actions", path, number, name, ref, "", "github-tags", "digest", version, skip="a SHA pin without a `# vX.Y.Z` comment, so there is no version to compare"))
            continue
        parsed = action_tag(version)
        if parsed is None:
            deps.append(Dependency("github-actions", path, number, name, ref, "", "github-tags", "branch", skip=f"`@{version}` is a branch or unknown ref: pin a tag or a full commit SHA"))
            continue
        style = "exact" if len(parsed.release) == 3 else "floating"
        deps.append(Dependency("github-actions", path, number, name, ref, version, "github-tags", style))
    return deps


def extract_npm(path: str, text: str) -> List[Dependency]:
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as error:
        raise ReleaseError(f"{path} is not valid JSON: line {error.lineno} column {error.colno}: {error.msg}") from None
    lines = text.splitlines()
    deps = []
    for section in NPM_SECTIONS:
        start = next((i for i, l in enumerate(lines) if f'"{section}"' in l), 0)
        for name, value in (manifest.get(section) or {}).items():
            number = next((i + 1 for i in range(start, len(lines)) if f'"{name}"' in lines[i]), 0)
            value = str(value).strip()
            if ":" in value or "/" in value:
                deps.append(Dependency("npm", path, number, name, value, "", "npm", "url", skip="a git, file, alias or URL dependency"))
                continue
            if value in ("", "*", "x", "latest"):
                deps.append(Dependency("npm", path, number, name, value, "", "npm", "unpinned"))
                continue
            try:
                ranges = npm_range(value)
            except ReleaseError as error:
                deps.append(Dependency("npm", path, number, name, value, "", "npm", "unknown", skip=str(error)))
                continue
            floors = [t for op, t in ranges[0] if op in (">=", "=")]
            style = "exact" if npm_semver(value) else "range"
            if not floors:
                deps.append(Dependency("npm", path, number, name, value, "", "npm", style, skip="no lower bound to compare with"))
                continue
            deps.append(Dependency("npm", path, number, name, value, str(floors[0]), "npm", style))
    return deps


EXTRACTORS = {"pip": extract_pip, "dockerfile": extract_dockerfile, "github-actions": extract_actions, "npm": extract_npm}


def scan(root: str) -> List[Dependency]:
    deps = []
    for rel in discover(root):
        with open(os.path.join(root, rel), encoding="utf-8") as handle:
            deps += EXTRACTORS[manager_for(rel)](rel, handle.read())
    return deps


def available_for(available: Dict[str, Any], dep: Dependency) -> Dict[str, str]:
    table = available.get(dep.datasource) or {}
    key = normalise(dep.name) if dep.datasource == "pypi" else dep.name
    entries = next((v for k, v in table.items() if (normalise(k) if dep.datasource == "pypi" else k) == key), None)
    if entries is None:
        return {}
    return {str(v): "" for v in entries} if isinstance(entries, list) else {str(k): str(v) for k, v in entries.items()}


def compatible(current: Version, candidate: Version, datasource: str) -> bool:
    if datasource == "docker":
        return candidate.suffix == current.suffix and len(candidate.release) == len(current.release)
    if datasource == "github-tags":
        return len(candidate.release) == len(current.release)
    return True


def compatibility_line(release: Sequence[int]) -> Tuple[int, ...]:
    padded = pad(release, 3)
    for index, number in enumerate(padded):
        if number:
            return padded[: index + 1]
    return padded


def classify(current: Version, new: Version) -> Tuple[str, bool]:
    a, b = pad(current.release, 3), pad(new.release, 3)
    kind = "major" if a[0] != b[0] else "minor" if a[1] != b[1] else "patch"
    return kind, compatibility_line(a) != compatibility_line(b)


def with_precision(old: str, new: Version) -> str:
    parts = old.split(".")
    numbers = [str(n) for n in pad(new.release, len(parts))]
    return ".".join(p if p in ("x", "X", "*") else numbers[i] for i, p in enumerate(parts))


def new_value(dep: Dependency, new: Version, digest: str) -> Tuple[str, bool]:
    if dep.datasource == "npm":
        in_range = npm_satisfies(npm_semver(new.text), npm_range(dep.value)) if dep.value else False
        simple = re.fullmatch(r"(\^|~|=|>=)?(v?)([0-9xX*]+(?:\.[0-9xX*]+){0,2})", dep.value)
        return (simple.group(1) or "") + with_precision(simple.group(3), new) if simple else "", in_range
    if dep.datasource == "pypi":
        specs = pip_specifiers(dep.value) if dep.value else []
        in_range = bool(specs) and pip_satisfies(new, specs)
        if len(specs) == 1 and specs[0][0] in ("==", "~=", ">="):
            return specs[0][0] + with_precision(specs[0][1], new), in_range
        return ("==" + new.text if not specs else ""), in_range
    if dep.datasource == "docker":
        ref = f"{dep.name}:{new.text}"
        if dep.digest:
            ref += f"@{digest or '<digest of ' + new.text + '>'}"
        return ref, False
    if dep.style == "digest":
        return f"{dep.value.partition('@')[0]}@{digest or '<sha of ' + new.text + '>'} # {new.text}", False
    return f"{dep.value.partition('@')[0]}@{new.text}", False


def make_update(dep: Dependency, current: Optional[Version], new: Version, digest: str) -> Update:
    value, in_range = new_value(dep, new, digest)
    if in_range and not value:
        value = dep.value
    if current is None:
        kind, breaking, group = "pin", False, "pin"
    else:
        kind, breaking = classify(current, new)
        group = "major" if breaking else kind
    notes = [dep.note] if dep.note else []
    if in_range and value != dep.value:
        notes.append("already allowed by the range: refreshing the lockfile installs it")
    if breaking and kind != "major":
        notes.append(f"0.x {kind} update: breaking by the 0.x rule")
    if group == "patch" and value != dep.value:
        notes.append("automerge candidate")
    if not value:
        notes.append("the range has several comparators: rewrite it by hand")
    return Update(dep.manager, dep.file, dep.line, dep.name, dep.value, value, current.text if current else "", new.text, kind, breaking, group, in_range, "; ".join(notes))


def propose(dep: Dependency, available: Dict[str, Any]) -> List[Update]:
    if dep.skip:
        return []
    parse = PARSERS[dep.datasource]
    entries = available_for(available, dep)
    versions = [(v, entries[t]) for t in entries for v in [parse(t)] if v is not None]
    if dep.style == "unpinned":
        stable = [(v, d) for v, d in versions if not v.pre and not v.suffix]
        if not stable:
            return []
        best, digest = max(stable, key=lambda item: (item[0].key, len(item[0].release)))
        return [make_update(dep, None, best, digest)]
    current = parse(dep.current)
    if current is None:
        return []
    newer = [(v, d) for v, d in versions if compatible(current, v, dep.datasource) and v.key > current.key and (current.pre or not v.pre)]
    updates = []
    line = compatibility_line(current.release)
    for compatible_line in (True, False):
        bucket = [(v, d) for v, d in newer if (compatibility_line(v.release) == line) == compatible_line]
        if bucket:
            best, digest = max(bucket, key=lambda item: item[0].key)
            updates.append(make_update(dep, current, best, digest))
    return updates


GROUP_ORDER = ("major", "minor", "patch", "pin")


def check_dependencies(root: str, available: Dict[str, Any]) -> Tuple[List[Dependency], List[Update]]:
    deps = scan(root)
    updates = [u for d in deps for u in propose(d, available)]
    return deps, sorted(updates, key=lambda u: (GROUP_ORDER.index(u.group), u.file, u.line, u.name))


def render_deps(deps: Sequence[Dependency], updates: Sequence[Update]) -> str:
    files = sorted({d.file for d in deps})
    lines = [f"minirelease deps: {len(deps)} dependencies in {len(files)} file(s), {len(updates)} update(s) proposed"]
    for group in GROUP_ORDER:
        chosen = [u for u in updates if u.group == group]
        if not chosen:
            continue
        lines += ["", f"{group} ({len(chosen)})"]
        for u in chosen:
            lines.append(f"  {u.file + ':' + str(u.line):<30} {u.manager:<15} {u.name:<26} {u.from_version or 'unpinned'} -> {u.to_version}")
            if u.new_value == u.current_value:
                parts = ["no edit: the range already allows it, refresh the lockfile"]
            elif u.new_value:
                parts = [f"edit: {u.current_value or u.name} => {u.new_value}"]
            else:
                parts = []
            lines.append("      " + "; ".join(parts + ([u.note] if u.note else [])))
    skipped = [d for d in deps if d.skip]
    if skipped:
        lines += ["", f"skipped ({len(skipped)})"]
        lines += [f"  {d.file + ':' + str(d.line):<30} {d.manager:<15} {d.name:<26} {d.skip}" for d in skipped]
    return "\n".join(lines)


def read_json(path: str, what: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as error:
        raise ReleaseError(f"cannot read {what} {path}: {error.strerror}") from None
    except json.JSONDecodeError as error:
        raise ReleaseError(f"{what} {path} is not valid JSON: line {error.lineno} column {error.colno}: {error.msg}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minirelease", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--repo", default=".", help="Git repository (default: current directory)")
    shared.add_argument("--tag-prefix", default="v", help="release tags are PREFIX + SemVer (default: v)")
    shared.add_argument("--json", action="store_true", help="machine-readable output")
    bump = argparse.ArgumentParser(add_help=False)
    bump.add_argument("--pre", default="", metavar="CHANNEL", help="cut a pre-release on this channel (alpha, beta, rc)")
    bump.add_argument("--breaking-always-bump-major", action="store_true", help="leave 0.x on a breaking change (git-cliff's option of the same name)")
    bump.add_argument("--features-always-bump-minor", action="store_true", help="a feature bumps the minor version even in 0.x")
    bump.add_argument("--initial", default="0.1.0", help="first version when there is no tag yet")
    log = sub.add_parser("log", parents=[shared], help="commits since the latest tag, parsed and classified")
    log.add_argument("--strict", action="store_true", help="exit 1 if any commit is not conventional")
    sub.add_parser("next", parents=[shared, bump], help="print the next version; exit 1 if there is nothing to release")
    changelog = sub.add_parser("changelog", parents=[shared, bump], help="the whole changelog in Keep a Changelog format")
    changelog.add_argument("--bump", action="store_true", help="name the unreleased section after the next version")
    changelog.add_argument("--compare-url", default="", help="https://github.com/OWNER/REPO for the comparison links")
    release = sub.add_parser("release", parents=[shared, bump], help="write the changelog, optionally commit it, and create the annotated tag")
    release.add_argument("--changelog", default="", metavar="FILE", help="write the changelog to FILE inside the repository")
    release.add_argument("--commit", action="store_true", help="commit the changelog as chore(release): TAG before tagging")
    release.add_argument("--sign", action="store_true", help="sign the tag (git tag -s; uses your gpg.format and user.signingkey)")
    release.add_argument("--compare-url", default="", help="https://github.com/OWNER/REPO for the comparison links")
    release.add_argument("--dry-run", action="store_true", help="print the tag message and change nothing")
    deps = sub.add_parser("deps", help="propose dependency updates from a fixture of available versions")
    deps.add_argument("root", help="directory to scan for requirements*.txt, Dockerfile, workflows and package.json")
    deps.add_argument("--available", required=True, help="JSON: {datasource: {name: [versions] or {version: digest}}}")
    deps.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv: Optional[Sequence[str]] = None, runner: Runner = subprocess_runner, today: str = "") -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    today = today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    try:
        if args.command == "deps":
            if not os.path.isdir(args.root):
                raise ReleaseError(f"{args.root} is not a directory")
            found, updates = check_dependencies(args.root, read_json(args.available, "available-versions file"))
            if args.json:
                print(json.dumps({"dependencies": [asdict(d) for d in found], "updates": [asdict(u) for u in updates]}, indent=2))
            else:
                print(render_deps(found, updates))
            return 1 if updates else 0
        history = load_history(args.repo, args.tag_prefix, runner)
        if args.command == "log":
            bad = [c for c in history.unreleased.commits if c.conventional is None]
            print(json.dumps([commit_json(c) for c in history.unreleased.commits], indent=2) if args.json else render_log(history))
            return 1 if args.strict and bad else 0
        policy = Policy(args.breaking_always_bump_major, args.features_always_bump_minor, args.initial)
        decision = decide(history, policy, args.pre)
        if args.command == "next":
            if args.json:
                print(json.dumps(decision_json(history, decision, args.tag_prefix), indent=2))
            elif decision.version:
                print(args.tag_prefix + str(decision.version))
            if decision.version is None:
                print(f"minirelease: {decision.reason}", file=sys.stderr)
                return 1
            print(f"minirelease: {decision.reason}", file=sys.stderr)
            return 0
        if args.command == "changelog":
            releases = with_next(history, decision, args.tag_prefix, today) if args.bump else history.releases
            if args.json:
                print(json.dumps([release_json(r) for r in releases], indent=2))
            else:
                sys.stdout.write(render_changelog(releases, args.compare_url))
            return 0
        if args.commit and not args.changelog:
            raise ReleaseError("--commit needs --changelog FILE: there is nothing else to commit")
        if decision.version is None:
            print(f"minirelease: {decision.reason}", file=sys.stderr)
            return 1
        return run_release(args, history, decision, runner, today)
    except ReleaseError as error:
        print(f"minirelease: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
