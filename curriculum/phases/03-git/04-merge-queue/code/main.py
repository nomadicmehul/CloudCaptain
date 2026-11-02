"""mergequeue: branch protection as data, CODEOWNERS matching, a speculative merge queue over real Git repositories and a branch-lifetime simulator."""
from __future__ import annotations

import argparse
import bisect
import itertools
import json
import math
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Deque, Dict, List, Optional, Sequence, Set, Tuple

CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
OWNER = re.compile(r"@[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?|[^@\s]+@[^@\s]+\.[^@\s]+")
METHODS = ("merge", "squash", "rebase")
PASSING = ("success", "neutral", "skipped")
DECISIONS = ("APPROVED", "CHANGES_REQUESTED")
REF_RULES = ("creation", "update", "deletion", "non_fast_forward")
QUEUE_NAME = "mergequeue"
QUEUE_EMAIL = "mergequeue@example.invalid"


class QueueError(Exception):
    pass


class BaseMoved(Exception):
    pass


@dataclass
class Rule:
    line: int
    pattern: str
    owners: List[str]
    negated: bool = False
    dir_only: bool = False
    final_star: bool = False
    regex: Any = field(default=None, repr=False, compare=False)


@dataclass
class LineProblem:
    line: int
    text: str
    reason: str


def glob_to_regex(body: str, classes: bool) -> str:
    out: List[str] = []
    i = 0
    while i < len(body):
        if body.startswith("**/", i) and (i == 0 or body[i - 1] == "/"):
            out.append("(?:.*/)?")
            i += 3
        elif body.startswith("/**", i) and i + 3 == len(body):
            out.append("/.*")
            i += 3
        elif body[i] == "*":
            while i < len(body) and body[i] == "*":
                i += 1
            out.append("[^/]*")
        elif body[i] == "?":
            out.append("[^/]")
            i += 1
        elif body[i] == "[" and classes and body.find("]", i + 2) != -1:
            end = body.find("]", i + 2)
            inner = body[i + 1:end]
            negate = inner[:1] in ("!", "^")
            out.append("[" + ("^" if negate else "") + (inner[1:] if negate else inner).replace("\\", "\\\\") + "]")
            i = end + 1
        elif body[i] == "\\" and i + 1 < len(body):
            out.append(re.escape(body[i + 1]))
            i += 2
        else:
            out.append(re.escape(body[i]))
            i += 1
    return "".join(out)


def compile_rule(line: int, pattern: str, owners: Sequence[str], gitignore: bool) -> Rule:
    text = pattern
    negated = gitignore and text.startswith("!")
    if negated:
        text = text[1:]
    dir_only = text.endswith("/")
    body = text.rstrip("/") if dir_only else text
    anchored = "/" in body
    if body.startswith("/"):
        body = body[1:]
    regex = ("" if anchored else "(?:.*/)?") + glob_to_regex(body, classes=gitignore)
    return Rule(line, pattern, list(owners), negated, dir_only, body.split("/")[-1] == "*", re.compile(regex))


def split_tokens(line: str) -> List[str]:
    return re.findall(r"(?:\\ |\S)+", line)


def codeowners_problem(pattern: str, owners: Sequence[str]) -> str:
    if pattern.startswith("!"):
        return "negation (`!`) is not supported in CODEOWNERS"
    if re.search(r"(?<!\\)\[", pattern):
        return "character ranges (`[...]`) are not supported in CODEOWNERS"
    bad = [owner for owner in owners if not OWNER.fullmatch(owner)]
    if bad:
        return f"{bad[0]!r} is not an owner: use @user, @org/team or an email address"
    return ""


def parse_codeowners(text: str) -> Tuple[List[Rule], List[LineProblem]]:
    rules: List[Rule] = []
    problems: List[LineProblem] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens = split_tokens(line)
        owners = list(itertools.takewhile(lambda token: not token.startswith("#"), tokens[1:]))
        reason = codeowners_problem(tokens[0], owners)
        if reason:
            problems.append(LineProblem(number, line, reason))
            continue
        rules.append(compile_rule(number, tokens[0], owners, gitignore=False))
    return rules, problems


def parse_gitignore(text: str) -> List[Rule]:
    rules: List[Rule] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r"(?<!\\) +$", "", raw)
        if line and not line.startswith("#"):
            rules.append(compile_rule(number, line, [], gitignore=True))
    return rules


def patterns_only(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        lines.append("" if not line or line.startswith("#") else split_tokens(line)[0])
    return "\n".join(lines) + "\n"


def parents(path: str) -> List[str]:
    parts = path.strip("/").split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def hits(rule: Rule, path: str, is_dir: bool) -> bool:
    return (is_dir or not rule.dir_only) and rule.regex.fullmatch(path) is not None


def codeowners_matches(rule: Rule, path: str) -> bool:
    if hits(rule, path, False):
        return True
    return not rule.final_star and any(hits(rule, directory, True) for directory in parents(path))


def owner_rule(rules: Sequence[Rule], path: str) -> Optional[Rule]:
    found = None
    for rule in rules:
        if codeowners_matches(rule, path):
            found = rule
    return found


def last_hit(rules: Sequence[Rule], path: str, is_dir: bool) -> Optional[Rule]:
    found = None
    for rule in rules:
        if hits(rule, path, is_dir):
            found = rule
    return found


def gitignore_rule(rules: Sequence[Rule], path: str) -> Optional[Rule]:
    for directory in parents(path):
        found = last_hit(rules, directory, True)
        if found is not None and not found.negated:
            return found
    return last_hit(rules, path, False)


@dataclass
class Difference:
    path: str
    codeowners: Optional[int]
    gitignore: Optional[int]
    why: str


def compare_semantics(text: str, paths: Sequence[str]) -> List[Difference]:
    rules, problems = parse_codeowners(text)
    ignore = parse_gitignore(patterns_only(text))
    invalid = {problem.line: problem.reason for problem in problems}
    found: List[Difference] = []
    for path in paths:
        mine, theirs = owner_rule(rules, path), gitignore_rule(ignore, path)
        a, b = (mine.line if mine else None), (theirs.line if theirs else None)
        if a == b:
            continue
        if theirs is not None and theirs.line in invalid:
            why = f".gitignore uses line {theirs.line} ({theirs.pattern}); CODEOWNERS skips it: {invalid[theirs.line]}"
        elif mine is None and theirs is not None and theirs.final_star:
            why = f"`{theirs.pattern}` covers only files directly inside in CODEOWNERS, and every level below in .gitignore"
        elif theirs is not None and not hits(theirs, path, False):
            why = f".gitignore stops at the directory matched by line {theirs.line}; CODEOWNERS keeps reading and the last match wins"
        else:
            why = "a different line matches last"
        found.append(Difference(path, a, b, why))
    return found


def find_codeowners(read: Any) -> Tuple[str, str]:
    for location in CODEOWNERS_LOCATIONS:
        text = read(location)
        if text is not None:
            return location, text
    return "", ""


@dataclass
class Review:
    user: str
    state: str
    commit_id: str = ""


@dataclass
class CommitFact:
    sha: str
    parents: int = 1
    signed: bool = False


@dataclass
class PullRequest:
    name: str
    title: str = ""
    author: str = ""
    head: str = ""
    files: List[str] = field(default_factory=list)
    commits: List[CommitFact] = field(default_factory=list)
    reviews: List[Review] = field(default_factory=list)
    checks: Dict[str, str] = field(default_factory=dict)
    up_to_date: bool = True
    unresolved_threads: int = 0
    last_pusher: str = ""


@dataclass
class QueueSettings:
    method: str = "merge"
    concurrency: int = 5
    timeout_minutes: int = 60


@dataclass
class Policy:
    name: str = ""
    enforced: bool = True
    pull_request: bool = False
    approvals: int = 0
    code_owner_review: bool = False
    dismiss_stale: bool = False
    last_push_approval: bool = False
    thread_resolution: bool = False
    allowed_methods: List[str] = field(default_factory=lambda: list(METHODS))
    required_checks: List[str] = field(default_factory=list)
    strict_checks: bool = False
    linear_history: bool = False
    signatures: bool = False
    queue: Optional[QueueSettings] = None
    ref_rules: List[str] = field(default_factory=list)
    not_evaluated: List[str] = field(default_factory=list)


@dataclass
class Violation:
    rule: str
    code: str
    message: str


def load_policy(data: Any) -> Policy:
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise QueueError('a ruleset is a JSON object with a "rules" list, the shape `gh api repos/OWNER/REPO/rulesets/ID` returns')
    policy = Policy(name=str(data.get("name", "")), enforced=data.get("enforcement", "active") == "active")
    for rule in data["rules"]:
        kind = str(rule.get("type", "")) if isinstance(rule, dict) else ""
        params = (rule.get("parameters") or {}) if isinstance(rule, dict) else {}
        if kind == "pull_request":
            policy.pull_request = True
            policy.approvals = int(params.get("required_approving_review_count", 0))
            policy.code_owner_review = bool(params.get("require_code_owner_review", False))
            policy.dismiss_stale = bool(params.get("dismiss_stale_reviews_on_push", False))
            policy.last_push_approval = bool(params.get("require_last_push_approval", False))
            policy.thread_resolution = bool(params.get("required_review_thread_resolution", False))
            if params.get("allowed_merge_methods"):
                policy.allowed_methods = [str(method).lower() for method in params["allowed_merge_methods"]]
        elif kind == "required_status_checks":
            policy.required_checks = [str(c["context"]) for c in params.get("required_status_checks") or [] if isinstance(c, dict) and c.get("context")]
            policy.strict_checks = bool(params.get("strict_required_status_checks_policy", False))
        elif kind == "required_linear_history":
            policy.linear_history = True
        elif kind == "required_signatures":
            policy.signatures = True
        elif kind == "merge_queue":
            method = str(params.get("merge_method", "MERGE")).lower()
            if method not in METHODS:
                raise QueueError(f"merge_queue.merge_method {method!r} is not one of MERGE, SQUASH, REBASE")
            policy.queue = QueueSettings(method, int(params.get("max_entries_to_build", 5)), int(params.get("check_response_timeout_minutes", 60)))
        elif kind in REF_RULES:
            policy.ref_rules.append(kind)
        else:
            policy.not_evaluated.append(kind or "?")
    return policy


def pr_from_json(data: Dict[str, Any], name: str = "") -> PullRequest:
    return PullRequest(
        name=str(data.get("name") or data.get("number") or name),
        title=str(data.get("title", "")),
        author=str(data.get("author", "")),
        head=str(data.get("head_sha", "")),
        files=[str(f) for f in data.get("files", [])],
        commits=[CommitFact(str(c.get("sha", "")), int(c.get("parents", 1)), bool(c.get("verified", False))) for c in data.get("commits", [])],
        reviews=[Review(str(r.get("user", "")), str(r.get("state", "")), str(r.get("commit_id", ""))) for r in data.get("reviews", [])],
        checks={str(k): str(v) for k, v in (data.get("checks") or {}).items()},
        up_to_date=bool(data.get("up_to_date", True)),
        unresolved_threads=int(data.get("unresolved_threads", 0)),
        last_pusher=str(data.get("last_pusher", "")),
    )


def review_state(pr: PullRequest, policy: Policy) -> Tuple[Set[str], Set[str], Set[str]]:
    decisions: Dict[str, str] = {}
    stale: Set[str] = set()
    for review in pr.reviews:
        state = review.state.upper()
        if review.user == pr.author:
            continue
        if state == "DISMISSED":
            decisions.pop(review.user, None)
        elif state == "APPROVED" and policy.dismiss_stale and review.commit_id and pr.head and review.commit_id != pr.head:
            decisions.pop(review.user, None)
            stale.add(review.user)
        elif state in DECISIONS:
            decisions[review.user] = state
            stale.discard(review.user)
    approvers = {user for user, state in decisions.items() if state == "APPROVED"}
    blocking = {user for user, state in decisions.items() if state == "CHANGES_REQUESTED"}
    return approvers, blocking, stale - approvers


def owner_logins(owner: str, teams: Dict[str, List[str]]) -> Set[str]:
    if owner in teams:
        return set(teams[owner])
    if owner.startswith("@") and "/" not in owner:
        return {owner[1:]}
    return set()


def missing_owner_approvals(pr: PullRequest, rules: Sequence[Rule], teams: Dict[str, List[str]], approvers: Set[str]) -> List[Tuple[Rule, List[str], Set[str]]]:
    missing: Dict[int, Tuple[Rule, List[str], Set[str]]] = {}
    for path in pr.files:
        rule = owner_rule(rules, path)
        if rule is None or not rule.owners:
            continue
        allowed: Set[str] = set()
        for owner in rule.owners:
            allowed |= owner_logins(owner, teams)
        if not allowed & approvers:
            missing.setdefault(rule.line, (rule, [], allowed))[1].append(path)
    return [missing[line] for line in sorted(missing)]


def evaluate(policy: Policy, pr: PullRequest, rules: Sequence[Rule] = (), teams: Optional[Dict[str, List[str]]] = None, method: str = "merge") -> List[Violation]:
    teams = teams or {}
    found: List[Violation] = []
    if policy.pull_request:
        approvers, blocking, stale = review_state(pr, policy)
        if len(approvers) < policy.approvals:
            note = f"; dismissed as stale after a newer push: {', '.join(sorted(stale))}" if stale else ""
            found.append(Violation("pull_request", "approvals", f"{len(approvers)} of {policy.approvals} required approvals{note}"))
        if blocking:
            found.append(Violation("pull_request", "changes-requested", f"changes requested by {', '.join(sorted(blocking))}"))
        if policy.code_owner_review:
            for rule, paths, allowed in missing_owner_approvals(pr, rules, teams, approvers):
                dismissed = sorted(allowed & stale)
                note = f"; the approval of {', '.join(dismissed)} was dismissed as stale" if dismissed else ""
                found.append(Violation("pull_request", "codeowners", f"{', '.join(paths)} {'needs' if len(paths) == 1 else 'need'} an approval from {' or '.join(rule.owners)} (CODEOWNERS line {rule.line}: {rule.pattern}){note}"))
        pusher = pr.last_pusher or pr.author
        if policy.last_push_approval and not approvers - {pusher}:
            found.append(Violation("pull_request", "last-push", f"the last push by {pusher} is not approved by anyone else"))
        if policy.thread_resolution and pr.unresolved_threads:
            found.append(Violation("pull_request", "threads", f"{pr.unresolved_threads} review thread(s) unresolved"))
        if method not in policy.allowed_methods:
            found.append(Violation("pull_request", "merge-method", f"{method} is not allowed; allowed: {', '.join(policy.allowed_methods)}"))
    for context in policy.required_checks:
        state = pr.checks.get(context)
        if state is None:
            found.append(Violation("required_status_checks", "checks", f"{context}: expected, no status reported (a workflow skipped by a path filter never reports)"))
        elif state.lower() not in PASSING:
            found.append(Violation("required_status_checks", "checks", f"{context}: {state}"))
    if policy.strict_checks and not pr.up_to_date:
        found.append(Violation("required_status_checks", "up-to-date", "the branch does not contain the latest base commit: update it, or let a merge queue test the combination"))
    if policy.linear_history and method == "merge":
        found.append(Violation("required_linear_history", "linear-history", "a merge commit would break linear history: use squash or rebase"))
    if policy.signatures:
        unsigned = [commit.sha[:7] for commit in pr.commits if not commit.signed]
        if unsigned:
            found.append(Violation("required_signatures", "signatures", f"unsigned or unverified commits: {', '.join(unsigned)}"))
    return found


@dataclass
class Build:
    commit: Optional[str]
    reason: str = ""


@dataclass
class Check:
    ok: bool
    detail: str = ""


@dataclass
class QueueReport:
    base: str = "main"
    method: str = "merge"
    concurrency: int = 1
    batch: int = 1
    before: str = ""
    after: str = ""
    merged: List[str] = field(default_factory=list)
    ejected: Dict[str, str] = field(default_factory=dict)
    blocked: Dict[str, List[str]] = field(default_factory=dict)
    rounds: int = 0
    runs: int = 0
    events: List[Dict[str, Any]] = field(default_factory=list)


def chunks(items: Sequence[str], size: int) -> List[List[str]]:
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def build_group(backend: Any, parent: str, group: Sequence[str], report: QueueReport) -> Tuple[List[str], str]:
    kept: List[str] = []
    head = parent
    for name in group:
        built = backend.build(head, name)
        if built.commit is None:
            report.ejected[name] = built.reason
            report.events.append({"round": report.rounds, "event": "eject", "prs": [name], "reason": built.reason})
        else:
            head = built.commit
            kept.append(name)
    return kept, head


def run_queue(entries: Sequence[str], backend: Any, concurrency: int = 1, batch: int = 1, report: Optional[QueueReport] = None, max_rounds: int = 1000) -> QueueReport:
    if concurrency < 1 or batch < 1:
        raise QueueError("concurrency and batch must be at least 1")
    report = report or QueueReport(concurrency=concurrency, batch=batch)
    todo: Deque[List[str]] = deque(chunks(entries, batch))
    tip = backend.tip()
    report.before = report.before or tip
    while todo:
        if report.rounds >= max_rounds:
            raise QueueError(f"the queue did not drain after {max_rounds} rounds")
        report.rounds += 1
        window: List[Tuple[List[str], str]] = []
        parent = tip
        while todo and len(window) < concurrency:
            kept, head = build_group(backend, parent, todo.popleft(), report)
            if kept:
                window.append((kept, head))
                report.events.append({"round": report.rounds, "event": "build", "prs": kept, "candidate": head, "parent": parent})
                parent = head
        if not window:
            continue
        with ThreadPoolExecutor(max_workers=len(window)) as pool:
            results = list(pool.map(lambda item: backend.test(item[1], item[0]), window))
        report.runs += len(window)
        for index, ((group, candidate), result) in enumerate(zip(window, results)):
            report.events.append({"round": report.rounds, "event": "pass" if result.ok else "fail", "prs": group, "candidate": candidate, "detail": result.detail})
            if result.ok:
                try:
                    backend.land(tip, candidate)
                except BaseMoved as moved:
                    tip = backend.tip()
                    todo.extendleft(reversed([g for g, _ in window[index:]]))
                    report.events.append({"round": report.rounds, "event": "rebuild", "prs": group, "reason": f"the base moved: {moved}"})
                    break
                tip = candidate
                report.merged.extend(group)
                report.events.append({"round": report.rounds, "event": "land", "prs": group, "candidate": candidate})
                continue
            rest = [g for g, _ in window[index + 1:]]
            if len(group) == 1:
                report.ejected[group[0]] = "check failed: " + result.detail
                report.events.append({"round": report.rounds, "event": "eject", "prs": group, "reason": "check failed: " + result.detail})
                front = rest
            else:
                half = (len(group) + 1) // 2
                front = [group[:half], group[half:]] + rest
                report.events.append({"round": report.rounds, "event": "split", "prs": group, "into": [group[:half], group[half:]]})
            todo.extendleft(reversed(front))
            break
    report.after = tip
    return report


class GitRepo:
    def __init__(self, path: str):
        self.path = path

    def run(self, *args: str, env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
        try:
            done = subprocess.run(["git", "-C", self.path, *args], capture_output=True, text=True, env={**os.environ, **(env or {})}, check=False)
        except FileNotFoundError as error:
            raise QueueError("git is not installed or not on PATH") from error
        return done.returncode, done.stdout, done.stderr

    def git(self, *args: str, env: Optional[Dict[str, str]] = None) -> str:
        code, out, err = self.run(*args, env=env)
        if code != 0:
            raise QueueError(f"`git {' '.join(args[:3])}` failed (exit {code}): {(err or out).strip()}")
        return out

    def show(self, rev: str, path: str) -> Optional[str]:
        code, out, _ = self.run("show", f"{rev}:{path}")
        return out if code == 0 else None

    def version(self) -> Tuple[int, ...]:
        match = re.search(r"(\d+)\.(\d+)", self.git("version"))
        return tuple(int(part) for part in match.groups()) if match else (0, 0)


def identity(name: str, email: str, date: str = "") -> Dict[str, str]:
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": QUEUE_NAME, "GIT_COMMITTER_EMAIL": QUEUE_EMAIL}
    if date:
        env["GIT_AUTHOR_DATE"] = date
    return env


class GitBackend:
    def __init__(self, repo: GitRepo, base: str, method: str, check: str = "", timeout: float = 600.0, dry_run: bool = False):
        self.repo, self.base, self.method, self.check, self.timeout, self.dry_run = repo, base, method, check, timeout, dry_run
        self.heads: Dict[str, str] = {}
        self.titles: Dict[str, str] = {}
        self.lock = threading.Lock()
        self.scratch = tempfile.mkdtemp(prefix="mergequeue-")

    def tip(self) -> str:
        return self.repo.git("rev-parse", "--verify", f"refs/heads/{self.base}^{{commit}}").strip()

    def merge_tree(self, *args: str) -> Tuple[Optional[str], List[str]]:
        code, out, err = self.repo.run("merge-tree", "--write-tree", "--name-only", "--no-messages", *args)
        lines = out.splitlines()
        if code == 0 and lines:
            return lines[0], []
        if code == 1:
            return None, sorted({line for line in lines[1:] if line})
        raise QueueError(f"git merge-tree failed (exit {code}): {err.strip()}")

    def build(self, parent: str, name: str) -> Build:
        head = self.heads[name]
        if self.method == "rebase":
            commit, reason = self.replay(parent, head)
        else:
            tree, conflicts = self.merge_tree(parent, head)
            commit, reason = (self.commit(tree, parent, name, head), "") if tree else (None, "conflict in " + ", ".join(conflicts))
        if commit:
            self.repo.git("update-ref", f"refs/mergequeue/{self.base}/{name}", commit)
        return Build(commit, reason)

    def commit(self, tree: str, parent: str, name: str, head: str) -> str:
        if self.method == "merge":
            message = f"Merge branch '{name}' into {self.base} (mergequeue)"
            return self.repo.git("commit-tree", "--no-gpg-sign", tree, "-p", parent, "-p", head, "-m", message, env=identity(QUEUE_NAME, QUEUE_EMAIL)).strip()
        rows = [line.split("\x1f") for line in self.repo.git("log", "--reverse", "--format=%an%x1f%ae%x1f%aI%x1f%s", f"{parent}..{head}").splitlines() if line]
        first = rows[0] if rows else [QUEUE_NAME, QUEUE_EMAIL, "", name]
        subject = self.titles.get(name) or (rows[-1][3] if rows else name)
        message = f"{subject} ({name})\n\n" + "\n".join(f"* {row[3]}" for row in rows)
        return self.repo.git("commit-tree", "--no-gpg-sign", tree, "-p", parent, "-m", message, env=identity(*first[:3])).strip()

    def replay(self, parent: str, head: str) -> Tuple[Optional[str], str]:
        current = parent
        for row in filter(None, self.repo.git("rev-list", "--reverse", "--topo-order", "--parents", f"{parent}..{head}").splitlines()):
            sha, *above = row.split()
            if len(above) != 1:
                return None, f"{sha[:7]} has {len(above)} parents; a rebase merge replays single-parent commits only"
            tree, conflicts = self.merge_tree(f"--merge-base={above[0]}", current, sha)
            if tree is None:
                return None, f"conflict replaying {sha[:7]} in {', '.join(conflicts)}"
            if tree == self.repo.git("rev-parse", f"{current}^{{tree}}").strip():
                continue
            name, email, date, message = self.repo.git("log", "-1", "--format=%an%x1f%ae%x1f%aI%x1f%B", sha).split("\x1f", 3)
            current = self.repo.git("commit-tree", "--no-gpg-sign", tree, "-p", current, "-m", message.rstrip("\n"), env=identity(name, email, date)).strip()
        return current, ""

    def test(self, commit: str, names: Sequence[str]) -> Check:
        if not self.check:
            return Check(True, "no check configured")
        with self.lock:
            path = tempfile.mkdtemp(prefix="candidate-", dir=self.scratch)
            self.repo.git("worktree", "add", "--detach", "--quiet", path, commit)
        env = {**os.environ, "MERGEQUEUE_BASE": self.base, "MERGEQUEUE_HEAD_SHA": commit, "MERGEQUEUE_PRS": " ".join(names)}
        try:
            done = subprocess.run(self.check, shell=True, cwd=path, capture_output=True, text=True, env=env, timeout=self.timeout, check=False)
            lines = [line for line in (done.stderr + "\n" + done.stdout).splitlines() if line.strip()]
            detail = lines[0][:200] if lines else f"exit {done.returncode}"
            return Check(done.returncode == 0, detail if done.returncode else "exit 0")
        except subprocess.TimeoutExpired:
            return Check(False, f"timed out after {self.timeout:g}s")
        finally:
            with self.lock:
                self.repo.run("worktree", "remove", "--force", path)

    def land(self, old: str, new: str) -> None:
        if self.dry_run:
            return
        code, _, err = self.repo.run("update-ref", "-m", "mergequeue: fast-forward", f"refs/heads/{self.base}", new, old)
        if code != 0:
            raise BaseMoved(err.strip())

    def cleanup(self, keep_refs: bool = False) -> None:
        if not keep_refs:
            for ref in self.repo.git("for-each-ref", "--format=%(refname)", f"refs/mergequeue/{self.base}/").split():
                self.repo.run("update-ref", "-d", ref)
        self.repo.run("worktree", "prune")
        shutil.rmtree(self.scratch, ignore_errors=True)


def pr_from_git(repo: GitRepo, tip: str, name: str, facts: Dict[str, Any]) -> PullRequest:
    code, out, _ = repo.run("rev-parse", "--verify", "--quiet", f"refs/heads/{name}^{{commit}}")
    if code != 0:
        raise QueueError(f"no branch {name!r} in {repo.path}")
    head = out.strip()
    base = repo.git("merge-base", tip, head).strip()
    commits = []
    authors = []
    for line in repo.git("log", "--reverse", "--format=%H%x1f%P%x1f%G?%x1f%an", f"{base}..{head}").splitlines():
        sha, above, signature, author = line.split("\x1f")
        commits.append(CommitFact(sha, len(above.split()), signature in ("G", "U")))
        authors.append(author)
    pr = pr_from_json(facts, name)
    pr.name, pr.head, pr.commits = name, head, commits
    pr.files = [path for path in repo.git("diff", "--name-only", "--no-renames", base, head).splitlines() if path]
    pr.author = pr.author or (authors[0] if authors else "")
    pr.up_to_date = repo.run("merge-base", "--is-ancestor", tip, head)[0] == 0
    return pr


def read_json(path: str, what: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as error:
        raise QueueError(f"cannot read the {what} {path}: {error.strerror}") from error
    except json.JSONDecodeError as error:
        raise QueueError(f"the {what} {path} is not valid JSON: {error}") from error


def queue_command(args: argparse.Namespace) -> QueueReport:
    repo = GitRepo(args.repo)
    if len(set(args.branches)) != len(args.branches):
        raise QueueError("a branch is queued twice")
    policy = load_policy(read_json(args.ruleset, "ruleset")) if args.ruleset else None
    settings = policy.queue if policy and policy.queue else None
    method = args.method or (settings.method if settings else "merge")
    concurrency = args.concurrency or (settings.concurrency if settings else 1)
    timeout = args.check_timeout or (settings.timeout_minutes * 60.0 if settings else 600.0)
    if repo.version() < ((2, 40) if method == "rebase" else (2, 38)):
        raise QueueError("mergequeue needs git 2.38 or later for merge-tree --write-tree (2.40 for --method rebase)")
    if not args.dry_run and repo.git("rev-parse", "--is-bare-repository").strip() == "false" and repo.run("symbolic-ref", "-q", "HEAD")[1].strip() == f"refs/heads/{args.base}":
        raise QueueError(f"refs/heads/{args.base} is checked out in {args.repo}; the queue moves that ref and would leave the working tree behind. Use a bare clone (git clone --bare) or detach HEAD first.")
    facts = read_json(args.prs, "pull request facts") if args.prs else {}
    teams = read_json(args.teams, "teams file") if args.teams else {}
    backend = GitBackend(repo, args.base, method, args.check or "", timeout, args.dry_run)
    report = QueueReport(base=args.base, method=method, concurrency=concurrency, batch=args.batch)
    try:
        tip = backend.tip()
        rules = parse_codeowners(find_codeowners(lambda location: repo.show(tip, location))[1])[0]
        admitted = []
        for name in args.branches:
            pr = pr_from_git(repo, tip, name, facts.get(name, {}))
            backend.heads[name] = pr.head
            backend.titles[name] = pr.title
            violations = evaluate(policy, pr, rules, teams, method) if policy else []
            if violations and policy and policy.enforced:
                report.blocked[name] = [f"{v.code}: {v.message}" for v in violations]
            else:
                admitted.append(name)
        return run_queue(admitted, backend, concurrency, args.batch, report)
    finally:
        backend.cleanup(args.keep_refs)


@dataclass
class SimConfig:
    developers: int = 8
    branch_hours: float = 8.0
    days: int = 30
    hours_per_day: float = 6.0
    regions: int = 10000
    regions_per_hour: float = 0.5
    hotness: float = 0.6
    ci_minutes: float = 20.0
    concurrency: int = 1
    failure_rate: float = 0.05
    rework_minutes: float = 30.0
    seed: int = 7


@dataclass
class SimBranch:
    ident: int
    developer: int
    start: float
    submit: float
    regions: Set[int]
    broken: bool


@dataclass
class SimResult:
    developers: int
    branch_hours: float
    concurrency: int
    prs: int
    conflict_rate: float
    rework_share: float
    wait_p50_minutes: float
    wait_p90_minutes: float
    ci_runs_per_pr: float
    load: float


def generate_branches(cfg: SimConfig, rng: random.Random) -> List[SimBranch]:
    cumulative = list(itertools.accumulate(1.0 / rank ** cfg.hotness for rank in range(1, cfg.regions + 1)))
    horizon = cfg.days * cfg.hours_per_day
    branches: List[SimBranch] = []
    for developer in range(cfg.developers):
        start = -rng.uniform(0, cfg.branch_hours)
        while True:
            length = cfg.branch_hours * rng.uniform(0.5, 1.5)
            if start + length > horizon:
                break
            count = max(1, round(cfg.regions_per_hour * length * rng.uniform(0.5, 1.5)))
            touched = set(rng.choices(range(cfg.regions), cum_weights=cumulative, k=count))
            broken = rng.random() < cfg.failure_rate
            if start + length > 0:
                branches.append(SimBranch(0, developer, start, start + length, touched, broken))
            start += length
    branches.sort(key=lambda b: (b.submit, b.developer))
    for ident, branch in enumerate(branches):
        branch.ident = ident
    return branches


def queue_times(arrivals: Sequence[Tuple[float, int, bool]], ci_hours: float, concurrency: int) -> Tuple[Dict[int, Tuple[float, bool]], int]:
    outcome: Dict[int, Tuple[float, bool]] = {}
    waiting: Deque[Tuple[int, float, bool]] = deque()
    window: List[List[Any]] = []
    now, runs, index = 0.0, 0, 0
    while index < len(arrivals) or waiting or window:
        while waiting and len(window) < concurrency:
            ident, arrived, broken = waiting.popleft()
            window.append([ident, now, broken, arrived])
            runs += 1
        next_arrival = arrivals[index][0] if index < len(arrivals) else math.inf
        head_done = window[0][1] + ci_hours if window else math.inf
        if next_arrival <= head_done:
            now = max(now, next_arrival)
            waiting.append((arrivals[index][1], arrivals[index][0], arrivals[index][2]))
            index += 1
            continue
        now = head_done
        ident, _, broken, arrived = window.pop(0)
        outcome[ident] = (now - arrived, not broken)
        if broken:
            for entry in window:
                entry[1] = now
                runs += 1
    return outcome, runs


def overlaps(branches: Sequence[SimBranch], merged_at: Dict[int, float], left_at: Dict[int, float]) -> Dict[int, int]:
    landed = sorted((merged_at[b.ident], b.ident) for b in branches if b.ident in merged_at)
    times = [time for time, _ in landed]
    by_id = {b.ident: b for b in branches}
    result: Dict[int, int] = {}
    for branch in branches:
        window = landed[bisect.bisect_right(times, branch.start):bisect.bisect_left(times, left_at[branch.ident])]
        result[branch.ident] = sum(len(branch.regions & by_id[other].regions) for _, other in window if by_id[other].developer != branch.developer)
    return result


def percentile(values: Sequence[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)] if ordered else 0.0


def simulate(cfg: SimConfig) -> SimResult:
    branches = generate_branches(cfg, random.Random(cfg.seed))
    outcome, runs = queue_times([(b.submit, b.ident, b.broken) for b in branches], cfg.ci_minutes / 60.0, cfg.concurrency)
    left_at = {b.ident: b.submit + outcome[b.ident][0] for b in branches}
    merged_at = {ident: time for ident, time in left_at.items() if outcome[ident][1]}
    overlap = overlaps(branches, merged_at, left_at)
    work = sum(b.submit - b.start for b in branches) or 1.0
    waits = [outcome[ident][0] * 60.0 for ident in merged_at]
    count = len(branches) or 1
    return SimResult(
        developers=cfg.developers,
        branch_hours=cfg.branch_hours,
        concurrency=cfg.concurrency,
        prs=len(branches),
        conflict_rate=round(sum(1 for v in overlap.values() if v) / count, 3),
        rework_share=round(sum(overlap.values()) * cfg.rework_minutes / 60.0 / work, 3),
        wait_p50_minutes=round(statistics.median(waits), 1) if waits else 0.0,
        wait_p90_minutes=round(percentile(waits, 0.9), 1),
        ci_runs_per_pr=round(runs / count, 2),
        load=round(len(branches) / (cfg.days * cfg.hours_per_day) * cfg.ci_minutes / 60.0 / cfg.concurrency, 2),
    )


DEMO_FILES = {
    "app/__init__.py": "",
    "app/prices.py": "def total(items):\n    return sum(items)\n\n\ndef selftest():\n    assert total([2, 3]) == 5\n",
    "app/report.py": "from app import prices\n\n\ndef summary(items):\n    return f\"{len(items)} items, total {prices.total(items)}\"\n\n\ndef selftest():\n    assert summary([2, 3]) == \"2 items, total 5\"\n",
    "app/tax.py": "RATE = 0.19\n\n\ndef selftest():\n    assert 0 < RATE < 1\n",
    "config/settings.ini": "[server]\ntimeout = 30\nworkers = 4\n",
    "docs/guide.md": "# Guide\n\nRun the checks with `python3 check.py`.\n",
    "README.md": "# shop\n",
    ".github/CODEOWNERS": "*              @acme/maintainers\n/app/          @acme/backend\n/config/       @acme/sre\n*.md           @acme/docs\n",
    "check.py": (
        "import importlib\nimport pkgutil\nimport sys\n\nimport app\n\nfailed = 0\n"
        "for module in pkgutil.iter_modules(app.__path__):\n    try:\n"
        "        importlib.import_module(f\"app.{module.name}\").selftest()\n"
        "    except Exception as error:\n        failed += 1\n"
        "        print(f\"FAIL app.{module.name}: {type(error).__name__}: {error}\".rstrip(\": \"), file=sys.stderr)\n"
        "sys.exit(1 if failed else 0)\n"
    ),
}
DEMO_BRANCHES: List[Tuple[str, str, str, List[Tuple[str, Dict[str, str]]]]] = [
    ("rename-total", "ana", "bo", [("Rename prices.total to prices.sum_prices", {"app/prices.py": DEMO_FILES["app/prices.py"].replace("total", "sum_prices"), "app/report.py": DEMO_FILES["app/report.py"].replace("prices.total", "prices.sum_prices")})]),
    ("docs-guide", "cy", "dee", [("Explain the merge queue in the guide", {"docs/guide.md": DEMO_FILES["docs/guide.md"] + "\nEvery change lands through the merge queue.\n"})]),
    ("invoice", "bo", "ana", [("Add invoices", {"app/invoice.py": "from app import prices\n\n\ndef due(items):\n    return round(prices.total(items) * 1.19, 2)\n\n\ndef selftest():\n    assert due([10]) == 11.9\n"})]),
    ("timeout-60", "eve", "sam", [("Raise the timeout to 60", {"config/settings.ini": "[server]\ntimeout = 60\nworkers = 4\n"})]),
    ("timeout-45", "fay", "sam", [("Raise the timeout to 45", {"config/settings.ini": "[server]\ntimeout = 45\nworkers = 4\n"})]),
    ("bad-tax", "ana", "bo", [("Store the tax rate in percent", {"app/tax.py": DEMO_FILES["app/tax.py"].replace("0.19", "19")})]),
    ("readme", "dee", "cy", [("Describe the shop", {"README.md": "# shop\n\nA small shop backend used by the merge queue lesson.\n"})]),
    ("workers-8", "bo", "ana", [("Run eight workers", {"config/settings.ini": "[server]\ntimeout = 30\nworkers = 8\n"})]),
    ("stale-approval", "cy", "bo", [
        ("Add discounts", {"app/discount.py": "def apply(price, percent):\n    return price * (100 - percent) / 100\n\n\ndef selftest():\n    assert apply(200, 10) == 180\n"}),
        ("Round discounted prices", {"app/discount.py": "def apply(price, percent):\n    return round(price * (100 - percent) / 100, 2)\n\n\ndef selftest():\n    assert apply(200, 10) == 180\n"}),
    ]),
]
DEMO_TEAMS = {"@acme/maintainers": ["ana", "bo", "cy", "dee", "eve", "fay", "sam"], "@acme/backend": ["ana", "bo"], "@acme/sre": ["sam", "eve"], "@acme/docs": ["dee", "cy"]}
DEMO_RULESET = {
    "name": "protect-main",
    "target": "branch",
    "enforcement": "active",
    "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
    "rules": [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {"type": "required_linear_history"},
        {"type": "pull_request", "parameters": {"required_approving_review_count": 1, "dismiss_stale_reviews_on_push": True, "require_code_owner_review": True, "require_last_push_approval": False, "required_review_thread_resolution": False, "allowed_merge_methods": ["squash", "rebase"]}},
        {"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": False, "required_status_checks": [{"context": "test"}]}},
        {"type": "merge_queue", "parameters": {"merge_method": "SQUASH", "max_entries_to_build": 3, "min_entries_to_merge": 1, "max_entries_to_merge": 5, "min_entries_to_merge_wait_minutes": 5, "grouping_strategy": "ALLGREEN", "check_response_timeout_minutes": 10}},
    ],
    "bypass_actors": [],
}


def build_demo(directory: str, stamp: int = 1790000000) -> Dict[str, str]:
    work = os.path.join(directory, "work")
    os.makedirs(work, exist_ok=True)
    repo = GitRepo(work)
    clock = [stamp]

    def commit(author: str, files: Dict[str, str], message: str) -> str:
        for path, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(work, path)) or work, exist_ok=True)
            with open(os.path.join(work, path), "w", encoding="utf-8") as handle:
                handle.write(text)
        clock[0] += 3600
        env = {"GIT_AUTHOR_DATE": f"{clock[0]} +0000", "GIT_COMMITTER_DATE": f"{clock[0]} +0000"}
        repo.git("add", "-A")
        repo.git("-c", f"user.name={author}", "-c", f"user.email={author}@example.invalid", "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", message, env=env)
        return repo.git("rev-parse", "HEAD").strip()

    repo.git("-c", "init.defaultBranch=main", "init", "--quiet")
    commit("ana", DEMO_FILES, "Start the shop")
    facts: Dict[str, Any] = {}
    for name, author, reviewer, steps in DEMO_BRANCHES:
        repo.git("checkout", "--quiet", "-b", name, "main")
        shas = [commit(author, files, message) for message, files in steps]
        facts[name] = {"author": author, "reviews": [{"user": reviewer, "state": "APPROVED", "commit_id": shas[0]}], "checks": {"test": "success"}}
    repo.git("checkout", "--quiet", "--detach", "main")
    origin = os.path.join(directory, "origin.git")
    GitRepo(directory).git("clone", "--bare", "--quiet", work, origin)
    paths = {"origin": origin, "work": work, "prs": os.path.join(directory, "prs.json"), "ruleset": os.path.join(directory, "ruleset.json"), "teams": os.path.join(directory, "teams.json")}
    for key, data in (("prs", facts), ("ruleset", DEMO_RULESET), ("teams", DEMO_TEAMS)):
        with open(paths[key], "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
    return paths


def rule_json(rule: Optional[Rule]) -> Optional[Dict[str, Any]]:
    return None if rule is None else {"line": rule.line, "pattern": rule.pattern, "owners": rule.owners}


def render_owners(rows: Sequence[Tuple[str, Optional[Rule]]], problems: Sequence[LineProblem]) -> str:
    width = max([len(path) for path, _ in rows] + [4])
    lines = []
    for path, rule in rows:
        owners = " ".join(rule.owners) if rule and rule.owners else "(no owners)"
        where = f"line {rule.line}: {rule.pattern}" if rule else "no matching line"
        lines.append(f"{path:<{width}}  {owners:<36}  {where}")
    lines.extend(f"line {p.line} skipped: {p.reason}: {p.text}" for p in problems)
    return "\n".join(lines)


def render_verdict(pr: PullRequest, violations: Sequence[Violation], policy: Policy, method: str) -> str:
    lines = [f"{pr.name}: {'blocked' if violations and policy.enforced else 'mergeable'} by ruleset {policy.name or '(unnamed)'} with {method}"]
    lines.extend(f"  {v.code:<18} {v.message}" for v in violations)
    if violations and not policy.enforced:
        lines.append("  (enforcement is not active: violations are reported, nothing is blocked)")
    if policy.not_evaluated:
        lines.append(f"  not evaluated: {', '.join(policy.not_evaluated)}")
    return "\n".join(lines)


def render_report(report: QueueReport) -> str:
    lines = [f"queue for {report.base}: method {report.method}, concurrency {report.concurrency}, batch {report.batch}"]
    lines.extend(f"  blocked  {name:<16} {reason}" for name, reasons in report.blocked.items() for reason in reasons)
    for event in report.events:
        prs = "+".join(event["prs"])
        if event["event"] == "build":
            lines.append(f"round {event['round']}: build {prs:<24} {event['candidate'][:7]} on {event['parent'][:7]}")
        elif event["event"] in ("pass", "fail"):
            lines.append(f"round {event['round']}: {event['event']:<5} {prs:<24} {event['detail']}")
        elif event["event"] == "land":
            lines.append(f"round {event['round']}: land  {prs:<24} {report.base} -> {event['candidate'][:7]}")
        elif event["event"] == "split":
            lines.append(f"round {event['round']}: split {prs:<24} into {' and '.join('+'.join(g) for g in event['into'])}")
        else:
            lines.append(f"round {event['round']}: {event['event']:<5} {prs:<24} {event.get('reason', '')}")
    lines.append(f"merged {len(report.merged)}, ejected {len(report.ejected)}, blocked {len(report.blocked)} in {report.rounds} round(s) and {report.runs} CI run(s); {report.base} {report.before[:7]} -> {report.after[:7]}")
    return "\n".join(lines)


def render_simulation(results: Sequence[SimResult]) -> str:
    lines = ["devs  branch h  queue  PRs   conflicts  rework  wait p50  wait p90  CI runs/PR  load"]
    for r in results:
        lines.append(f"{r.developers:>4}  {r.branch_hours:>8g}  {r.concurrency:>5}  {r.prs:>4}  {r.conflict_rate:>9.0%}  {r.rework_share:>6.1%}  {r.wait_p50_minutes:>6.0f}m  {r.wait_p90_minutes:>6.0f}m  {r.ci_runs_per_pr:>10.2f}  {r.load:>4.2f}")
    return "\n".join(lines)


def numbers(text: str, kind: Any) -> List[Any]:
    try:
        return [kind(part) for part in text.split(",") if part.strip()]
    except ValueError as error:
        raise QueueError(f"not a comma-separated list of numbers: {text!r}") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mergequeue", description="Branch protection as data, CODEOWNERS matching, a speculative merge queue over a Git repository, and a branch-lifetime simulator.")
    sub = parser.add_subparsers(dest="command", required=True)
    owners = sub.add_parser("owners", help="who owns each path: CODEOWNERS, last match wins")
    owners.add_argument("paths", nargs="*", help="paths to look up (default: every file at --ref)")
    owners.add_argument("--codeowners", help="a CODEOWNERS file (default: .github/CODEOWNERS, CODEOWNERS or docs/CODEOWNERS at --ref)")
    owners.add_argument("--repo", default=".")
    owners.add_argument("--ref", default="HEAD")
    owners.add_argument("--stdin", action="store_true", help="read paths from stdin, one per line")
    mode = owners.add_mutually_exclusive_group()
    mode.add_argument("--gitignore", action="store_true", help="evaluate the patterns as .gitignore does, printed like git check-ignore -v -n")
    mode.add_argument("--compare", action="store_true", help="list the paths where CODEOWNERS and .gitignore semantics disagree")
    owners.add_argument("--strict", action="store_true", help="exit 1 when a path has no owner or a line is skipped")
    owners.add_argument("--json", action="store_true")
    protect = sub.add_parser("protect", help="evaluate one pull request against a ruleset")
    protect.add_argument("--ruleset", required=True)
    protect.add_argument("--pr", required=True, help="pull request facts as JSON")
    protect.add_argument("--codeowners")
    protect.add_argument("--teams", help="JSON map of @org/team or email to logins")
    protect.add_argument("--method", choices=METHODS, help="merge method (default: the ruleset's merge queue method, else merge)")
    protect.add_argument("--json", action="store_true")
    queue = sub.add_parser("queue", help="land branches through a speculative merge queue")
    queue.add_argument("branches", nargs="+", help="branches in queue order")
    queue.add_argument("--repo", required=True, help="the repository; a bare clone is safest")
    queue.add_argument("--base", default="main")
    queue.add_argument("--check", help="shell command run in a worktree of each candidate; exit 0 passes")
    queue.add_argument("--check-timeout", type=float, help="seconds (default: the ruleset's check_response_timeout_minutes, else 600)")
    queue.add_argument("--ruleset")
    queue.add_argument("--prs", help="JSON map of branch to author, reviews and checks")
    queue.add_argument("--teams")
    queue.add_argument("--method", choices=METHODS)
    queue.add_argument("--concurrency", type=int, help="candidates built and tested at once (default: the ruleset's max_entries_to_build, else 1)")
    queue.add_argument("--batch", type=int, default=1, help="pull requests per candidate; a failing batch is split in halves")
    queue.add_argument("--dry-run", action="store_true", help="build and test, never move the base branch")
    queue.add_argument("--keep-refs", action="store_true", help="keep refs/mergequeue/BASE/* for inspection")
    queue.add_argument("--json", action="store_true")
    sim = sub.add_parser("simulate", help="how branch lifetime and team size drive conflicts and queue wait")
    sim.add_argument("--developers", default="4,12,30")
    sim.add_argument("--branch-hours", default="4,40")
    sim.add_argument("--days", type=int, default=30)
    sim.add_argument("--ci-minutes", type=float, default=20.0)
    sim.add_argument("--concurrency", type=int, default=1)
    sim.add_argument("--failure-rate", type=float, default=0.05)
    sim.add_argument("--seed", type=int, default=7)
    sim.add_argument("--json", action="store_true")
    demo = sub.add_parser("demo", help="create the lesson's repository, ruleset, teams and pull request facts")
    demo.add_argument("directory")
    return parser


def owners_command(args: argparse.Namespace) -> int:
    repo = GitRepo(args.repo)
    if args.codeowners:
        try:
            with open(args.codeowners, encoding="utf-8") as handle:
                source, text = args.codeowners, handle.read()
        except OSError as error:
            raise QueueError(f"cannot read {args.codeowners}: {error.strerror}") from error
    else:
        source, text = find_codeowners(lambda location: repo.show(args.ref, location))
        if not source:
            raise QueueError(f"no CODEOWNERS at {args.ref} in {args.repo} (looked in {', '.join(CODEOWNERS_LOCATIONS)})")
    paths = [line.strip() for line in sys.stdin if line.strip()] if args.stdin else list(args.paths)
    if not paths:
        paths = [p for p in repo.git("ls-tree", "-r", "--name-only", args.ref).splitlines() if p]
    rules, problems = parse_codeowners(text)
    if args.gitignore:
        ignore = parse_gitignore(text if os.path.basename(source) == ".gitignore" else patterns_only(text))
        for path in paths:
            rule = gitignore_rule(ignore, path)
            print(f"{source}:{rule.line}:{rule.pattern}\t{path}" if rule else f"::\t{path}")
        return 0
    if args.compare:
        found = compare_semantics(text, paths)
        if args.json:
            print(json.dumps([asdict(d) for d in found], indent=2))
        else:
            print("\n".join(f"{d.path}: CODEOWNERS line {d.codeowners or '-'}, .gitignore line {d.gitignore or '-'}: {d.why}" for d in found) or "no differences")
        return 0
    rows = [(path, owner_rule(rules, path)) for path in paths]
    if args.json:
        print(json.dumps({"source": source, "paths": {path: rule_json(rule) for path, rule in rows}, "skipped": [asdict(p) for p in problems]}, indent=2))
    else:
        print(render_owners(rows, problems))
    unowned = [path for path, rule in rows if rule is None or not rule.owners]
    return 1 if args.strict and (unowned or problems) else 0


def protect_command(args: argparse.Namespace) -> int:
    policy = load_policy(read_json(args.ruleset, "ruleset"))
    pr = pr_from_json(read_json(args.pr, "pull request facts"))
    rules: List[Rule] = []
    if args.codeowners:
        try:
            with open(args.codeowners, encoding="utf-8") as handle:
                rules = parse_codeowners(handle.read())[0]
        except OSError as error:
            raise QueueError(f"cannot read {args.codeowners}: {error.strerror}") from error
    teams = read_json(args.teams, "teams file") if args.teams else {}
    method = args.method or (policy.queue.method if policy.queue else "merge")
    violations = evaluate(policy, pr, rules, teams, method)
    if args.json:
        print(json.dumps({"pr": pr.name, "method": method, "mergeable": not (violations and policy.enforced), "violations": [asdict(v) for v in violations], "not_evaluated": policy.not_evaluated}, indent=2))
    else:
        print(render_verdict(pr, violations, policy, method))
    return 1 if violations and policy.enforced else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "owners":
            return owners_command(args)
        if args.command == "protect":
            return protect_command(args)
        if args.command == "queue":
            report = queue_command(args)
            print(json.dumps(asdict(report), indent=2) if args.json else render_report(report))
            return 0 if not report.ejected and not report.blocked else 1
        if args.command == "simulate":
            results = [simulate(SimConfig(developers=d, branch_hours=h, days=args.days, ci_minutes=args.ci_minutes, concurrency=args.concurrency, failure_rate=args.failure_rate, seed=args.seed)) for d in numbers(args.developers, int) for h in numbers(args.branch_hours, float)]
            print(json.dumps([asdict(r) for r in results], indent=2) if args.json else render_simulation(results))
            return 0
        paths = build_demo(args.directory)
        print(f"created {paths['origin']} with {len(DEMO_BRANCHES)} branches; try:")
        print(f"  python3 {sys.argv[0] or 'main.py'} queue --repo {paths['origin']} --ruleset {paths['ruleset']} --prs {paths['prs']} --teams {paths['teams']} --check 'python3 check.py' {' '.join(b[0] for b in DEMO_BRANCHES)}")
        return 0
    except QueueError as error:
        print(f"mergequeue: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
