"""minibisect: bisection over a commit graph with skips and automated runs, line blame with ignored revisions and moves, pickaxe search, and recovery of lost commits."""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import re
import struct
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

Runner = Callable[..., Tuple[int, str, str]]
Marks = List[bool]
Fingerprint = Dict[int, int]

GOOD, BAD, SKIP = "good", "bad", "skip"
PRN_MODULO = 32768
MOVE_SCORE = 20
FINGERPRINT_FILE_THRESHOLD = 10
MAX_SEARCH_DISTANCE = 10
CERTAIN_NOTHING_MATCHES = -2
CERTAINTY_NOT_CALCULATED = -1
NO_MATCH, MATCH, MANY = 0, 1, 2
MAX_EQLIMIT = 1024
SIMSCAN_WINDOW = 100
KPDIS_RUN = 4
LINE_MAX = sys.maxsize
GIT_SPACE = frozenset(b" \t\n\r")
LOG_FORMAT = "%H%x00%P%x00%ct%x00%an%x00%ae%x00%ad%x00%cn%x00%ce%x00%cd%x00%s"
OBJECT_ID = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class BisectError(Exception):
    pass


@dataclass
class Commit:
    sha: str
    parents: Tuple[str, ...]
    date: int
    subject: str = ""
    author: str = ""
    author_mail: str = ""
    author_date: str = "0 +0000"
    committer: str = ""
    committer_mail: str = ""
    committer_date: str = "0 +0000"


@dataclass(frozen=True)
class Change:
    old_start: int
    old_count: int
    new_start: int
    new_count: int


@dataclass
class Step:
    state: str
    commit: Optional[str] = None
    remaining: int = 0
    steps: int = 0
    candidates: int = 0
    reaches: int = 0
    suspects: List[str] = field(default_factory=list)
    distances: Dict[str, int] = field(default_factory=dict)


@dataclass
class Trial:
    commit: str
    exit: int
    verdict: str


@dataclass
class Entry:
    lno: int
    count: int
    suspect: str
    s_lno: int
    ignored: bool = False
    unblamable: bool = False


@dataclass
class BlameLine:
    line: int
    commit: str
    orig_line: int
    text: str
    boundary: bool
    ignored: bool
    unblamable: bool
    previous: Optional[str]


@dataclass
class ReflogEntry:
    ref: str
    index: int
    old: str
    new: str
    message: str

    @property
    def name(self) -> str:
        return f"{self.ref}@{{{self.index}}}"


def split_lines(text: str) -> List[str]:
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def short(sha: str) -> str:
    return sha[:8] if OBJECT_ID.match(sha) else sha


def classify(old: Sequence[str], new: Sequence[str]) -> Tuple[List[int], List[int]]:
    ids: Dict[str, int] = {}
    return [ids.setdefault(line, len(ids)) for line in old], [ids.setdefault(line, len(ids)) for line in new]


def bogosqrt(n: int) -> int:
    root = 1
    while n > 0:
        root <<= 1
        n >>= 2
    return root


def common_ends(a: Sequence[int], b: Sequence[int]) -> Tuple[int, int, int]:
    limit = min(len(a), len(b))
    start = 0
    while start < limit and a[start] == b[start]:
        start += 1
    tail = 0
    while tail < limit - start and a[-1 - tail] == b[-1 - tail]:
        tail += 1
    return start, len(a) - tail, len(b) - tail


def run_length(kinds: Sequence[int], i: int, step: int, start: int, end: int) -> Tuple[int, int]:
    unmatched, many = 0, 1
    j = i + step
    while start <= j <= end:
        if kinds[j] == NO_MATCH:
            unmatched += 1
        elif kinds[j] == MANY:
            many += 1
        else:
            break
        j += step
    return unmatched, many


def lost_in_noise(kinds: Sequence[int], i: int, start: int, end: int) -> bool:
    start = max(start, i - SIMSCAN_WINDOW)
    end = min(end, i + SIMSCAN_WINDOW)
    unmatched_before, many_before = run_length(kinds, i, -1, start, end)
    if unmatched_before == 0:
        return False
    unmatched_after, many_after = run_length(kinds, i, 1, start, end)
    if unmatched_after == 0:
        return False
    many = many_before + many_after
    return many * KPDIS_RUN < many + unmatched_before + unmatched_after


def discard_unmatched(lines: Sequence[int], start: int, end: int, other: Counter, changed: Marks) -> List[int]:
    limit = min(bogosqrt(len(lines)), MAX_EQLIMIT)
    kinds = [NO_MATCH] * len(lines)
    for i in range(start, end):
        seen = other[lines[i]]
        kinds[i] = NO_MATCH if seen == 0 else MANY if seen >= limit else MATCH
    kept = []
    for i in range(start, end):
        if kinds[i] == MATCH or (kinds[i] == MANY and not lost_in_noise(kinds, i, start, end - 1)):
            kept.append(i)
        else:
            changed[i] = True
    return kept


def middle_snake(ha: Sequence[int], off1: int, lim1: int, hb: Sequence[int], off2: int, lim2: int) -> Tuple[int, int]:
    dmin, dmax = off1 - lim2, lim1 - off2
    fmid, bmid = off1 - off2, lim1 - lim2
    odd = (fmid - bmid) & 1
    fmin = fmax = fmid
    bmin = bmax = bmid
    shift = 1 - dmin
    forward = [-1] * (dmax - dmin + 3)
    backward = [LINE_MAX] * (dmax - dmin + 3)
    forward[fmid + shift] = off1
    backward[bmid + shift] = lim1
    while True:
        if fmin > dmin:
            fmin -= 1
            forward[fmin - 1 + shift] = -1
        else:
            fmin += 1
        if fmax < dmax:
            fmax += 1
            forward[fmax + 1 + shift] = -1
        else:
            fmax -= 1
        for d in range(fmax, fmin - 1, -2):
            if forward[d - 1 + shift] >= forward[d + 1 + shift]:
                i1 = forward[d - 1 + shift] + 1
            else:
                i1 = forward[d + 1 + shift]
            i2 = i1 - d
            while i1 < lim1 and i2 < lim2 and ha[i1] == hb[i2]:
                i1 += 1
                i2 += 1
            forward[d + shift] = i1
            if odd and bmin <= d <= bmax and backward[d + shift] <= i1:
                return i1, i2
        if bmin > dmin:
            bmin -= 1
            backward[bmin - 1 + shift] = LINE_MAX
        else:
            bmin += 1
        if bmax < dmax:
            bmax += 1
            backward[bmax + 1 + shift] = LINE_MAX
        else:
            bmax -= 1
        for d in range(bmax, bmin - 1, -2):
            if backward[d - 1 + shift] < backward[d + 1 + shift]:
                i1 = backward[d - 1 + shift]
            else:
                i1 = backward[d + 1 + shift] - 1
            i2 = i1 - d
            while i1 > off1 and i2 > off2 and ha[i1 - 1] == hb[i2 - 1]:
                i1 -= 1
                i2 -= 1
            backward[d + shift] = i1
            if not odd and fmin <= d <= fmax and i1 <= forward[d + shift]:
                return i1, i2


def myers(a: Sequence[int], b: Sequence[int]) -> Tuple[Marks, Marks]:
    changed_a, changed_b = [False] * len(a), [False] * len(b)
    start, end_a, end_b = common_ends(a, b)
    kept_a = discard_unmatched(a, start, end_a, Counter(b), changed_a)
    kept_b = discard_unmatched(b, start, end_b, Counter(a), changed_b)
    ha, hb = [a[i] for i in kept_a], [b[i] for i in kept_b]
    boxes = [(0, len(ha), 0, len(hb))]
    while boxes:
        off1, lim1, off2, lim2 = boxes.pop()
        while off1 < lim1 and off2 < lim2 and ha[off1] == hb[off2]:
            off1 += 1
            off2 += 1
        while off1 < lim1 and off2 < lim2 and ha[lim1 - 1] == hb[lim2 - 1]:
            lim1 -= 1
            lim2 -= 1
        if off1 == lim1:
            for k in range(off2, lim2):
                changed_b[kept_b[k]] = True
        elif off2 == lim2:
            for k in range(off1, lim1):
                changed_a[kept_a[k]] = True
        else:
            mid1, mid2 = middle_snake(ha, off1, lim1, hb, off2, lim2)
            boxes.append((mid1, lim1, mid2, lim2))
            boxes.append((off1, mid1, off2, mid2))
    return changed_a, changed_b


class Group:
    def __init__(self, changed: Marks) -> None:
        self.changed = changed
        self.size = len(changed)
        self.start = self.end = 0
        while self.end < self.size and changed[self.end]:
            self.end += 1

    def is_empty(self) -> bool:
        return self.start == self.end

    def next(self) -> bool:
        if self.end == self.size:
            return False
        self.start = self.end = self.end + 1
        while self.end < self.size and self.changed[self.end]:
            self.end += 1
        return True

    def previous(self) -> bool:
        if self.start == 0:
            return False
        self.start = self.end = self.start - 1
        while self.start > 0 and self.changed[self.start - 1]:
            self.start -= 1
        return True

    def slide_down(self, lines: Sequence[int]) -> bool:
        if self.end < self.size and lines[self.start] == lines[self.end]:
            self.changed[self.start] = False
            self.changed[self.end] = True
            self.start += 1
            self.end += 1
            while self.end < self.size and self.changed[self.end]:
                self.end += 1
            return True
        return False

    def slide_up(self, lines: Sequence[int]) -> bool:
        if self.start > 0 and lines[self.start - 1] == lines[self.end - 1]:
            self.start -= 1
            self.end -= 1
            self.changed[self.start] = True
            self.changed[self.end] = False
            while self.start > 0 and self.changed[self.start - 1]:
                self.start -= 1
            return True
        return False


def compact(lines: Sequence[int], changed: Marks, other_changed: Marks) -> None:
    group, other = Group(changed), Group(other_changed)
    while True:
        if not group.is_empty():
            while True:
                size = group.end - group.start
                aligned_end = -1
                while group.slide_up(lines):
                    other.previous()
                earliest_end = group.end
                if not other.is_empty():
                    aligned_end = group.end
                while group.slide_down(lines):
                    other.next()
                    if not other.is_empty():
                        aligned_end = group.end
                if size == group.end - group.start:
                    break
            if group.end != earliest_end and aligned_end != -1:
                while other.is_empty():
                    group.slide_up(lines)
                    other.previous()
        if not group.next():
            break
        other.next()


def build_script(changed_a: Marks, changed_b: Marks) -> List[Change]:
    changes = []
    i = j = 0
    while i < len(changed_a) or j < len(changed_b):
        if (i < len(changed_a) and changed_a[i]) or (j < len(changed_b) and changed_b[j]):
            start_i, start_j = i, j
            while i < len(changed_a) and changed_a[i]:
                i += 1
            while j < len(changed_b) and changed_b[j]:
                j += 1
            changes.append(Change(start_i, i - start_i, start_j, j - start_j))
        else:
            i += 1
            j += 1
    return changes


def diff(old: Sequence[str], new: Sequence[str]) -> List[Change]:
    a, b = classify(old, new)
    changed_a, changed_b = myers(a, b)
    compact(a, changed_a, changed_b)
    compact(b, changed_b, changed_a)
    return build_script(changed_a, changed_b)


class History:
    def __init__(self, commits: Dict[str, Commit]) -> None:
        self.commits = commits

    def tree(self, sha: str) -> Dict[str, str]:
        raise NotImplementedError

    def blob(self, blob_id: str) -> str:
        raise NotImplementedError

    def blob_id(self, sha: str, path: str) -> Optional[str]:
        return self.tree(sha).get(path)

    def lines(self, blob_id: str) -> List[str]:
        return split_lines(self.blob(blob_id))

    def commit(self, sha: str) -> Commit:
        if sha not in self.commits:
            raise BisectError(f"{sha} is not a commit in this history")
        return self.commits[sha]


class MemoryHistory(History):
    def __init__(self, commits: Dict[str, Commit], files: Dict[str, Dict[str, str]]) -> None:
        super().__init__(commits)
        self.blobs: Dict[str, str] = {}
        self.trees: Dict[str, Dict[str, str]] = {}
        for sha, tree in files.items():
            self.trees[sha] = {}
            for path, text in tree.items():
                blob_id = hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()
                self.blobs[blob_id] = text
                self.trees[sha][path] = blob_id

    def tree(self, sha: str) -> Dict[str, str]:
        return self.trees.get(sha, {})

    def blob(self, blob_id: str) -> str:
        return self.blobs[blob_id]


def subprocess_runner(argv: Sequence[str], stdin: Optional[str] = None, cwd: Optional[str] = None) -> Tuple[int, str, str]:
    try:
        done = subprocess.run(list(argv), input=stdin, capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", check=False, cwd=cwd)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: command not found"
    return done.returncode, done.stdout, done.stderr


def git(runner: Runner, repo: str, *args: str, stdin: Optional[str] = None) -> str:
    code, out, err = runner(["git", "-C", repo, *args], stdin=stdin)
    if code != 0:
        raise BisectError(f"`git {' '.join(args[:3])}` failed (exit {code}): {(err or out).strip()}")
    return out


def parse_log(text: str) -> Dict[str, Commit]:
    commits: Dict[str, Commit] = {}
    for line in text.splitlines():
        parts = line.split("\x00")
        if len(parts) != 10:
            continue
        sha, parents, date, author, author_mail, author_date, committer, committer_mail, committer_date, subject = parts
        commits[sha] = Commit(sha, tuple(parents.split()), int(date), subject, author, f"<{author_mail}>", author_date, committer, f"<{committer_mail}>", committer_date)
    return commits


class GitHistory(History):
    def __init__(self, runner: Runner, repo: str, revs: Sequence[str] = (), shas: Sequence[str] = ()) -> None:
        self.runner, self.repo = runner, repo
        commits: Dict[str, Commit] = {}
        if revs:
            commits.update(parse_log(git(runner, repo, "log", "--date=raw", f"--format={LOG_FORMAT}", *revs, "--")))
        if shas:
            commits.update(parse_log(git(runner, repo, "log", "--stdin", "--no-walk=unsorted", "--date=raw", f"--format={LOG_FORMAT}", stdin="\n".join(shas) + "\n")))
        super().__init__(commits)
        self.trees: Dict[str, Dict[str, str]] = {}
        self.blobs: Dict[str, str] = {}

    def tree(self, sha: str) -> Dict[str, str]:
        if sha not in self.trees:
            entries: Dict[str, str] = {}
            for record in git(self.runner, self.repo, "ls-tree", "-r", "-z", sha).split("\x00"):
                if "\t" in record:
                    meta, path = record.split("\t", 1)
                    mode, kind, oid = meta.split()
                    if kind == "blob":
                        entries[path] = oid
            self.trees[sha] = entries
        return self.trees[sha]

    def blob(self, blob_id: str) -> str:
        if blob_id not in self.blobs:
            self.blobs[blob_id] = git(self.runner, self.repo, "cat-file", "blob", blob_id)
        return self.blobs[blob_id]


def resolve(runner: Runner, repo: str, name: str) -> str:
    code, out, _ = runner(["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}"])
    if code != 0 or not out.strip():
        raise BisectError(f"{name} does not name a commit in {repo}")
    return out.strip()


def ancestors(commits: Dict[str, Commit], tips: Iterable[str]) -> Set[str]:
    seen: Set[str] = set()
    stack = list(tips)
    while stack:
        sha = stack.pop()
        if sha in seen or sha not in commits:
            continue
        seen.add(sha)
        stack.extend(commits[sha].parents)
    return seen


def date_order(commits: Dict[str, Commit], tips: Sequence[str]) -> List[str]:
    order: List[str] = []
    seen: Set[str] = set()
    queue: List[Tuple[int, int, str]] = []
    for sha in tips:
        if sha in commits and sha not in seen:
            seen.add(sha)
            heapq.heappush(queue, (-commits[sha].date, len(seen), sha))
    while queue:
        _, _, sha = heapq.heappop(queue)
        order.append(sha)
        for parent in commits[sha].parents:
            if parent in commits and parent not in seen:
                seen.add(parent)
                heapq.heappush(queue, (-commits[parent].date, len(seen), parent))
    return order


def merge_bases(commits: Dict[str, Commit], one: str, two: str) -> List[str]:
    common = ancestors(commits, [one]) & ancestors(commits, [two])
    below: Set[str] = set()
    for sha in common:
        below |= ancestors(commits, commits[sha].parents)
    return sorted(common - below)


def approx_halfway(weight: int, nr: int) -> bool:
    diff_from_half = 2 * weight - nr
    return diff_from_half in (-1, 0, 1) or abs(diff_from_half) < nr // 1024


def find_bisection(commits: Dict[str, Commit], order: Sequence[str], find_all: bool) -> Tuple[Optional[str], Dict[str, int]]:
    nr = len(order)
    candidates = set(order)
    weight: Dict[str, int] = {}
    counted = 0
    for sha in order:
        interesting = sum(1 for parent in commits[sha].parents if parent in candidates)
        weight[sha] = 1 if interesting == 0 else -1 if interesting == 1 else -2
        counted += interesting == 0
    for sha in order:
        if weight[sha] != -2:
            continue
        weight[sha] = len(ancestors(commits, [sha]) & candidates)
        if not find_all and approx_halfway(weight[sha], nr):
            return sha, weight
        counted += 1
    while counted < nr:
        for sha in order:
            if weight[sha] >= 0:
                continue
            known = next((p for p in commits[sha].parents if p in candidates and weight[p] >= 0), None)
            if known is None:
                continue
            weight[sha] = weight[known] + 1
            counted += 1
            if not find_all and approx_halfway(weight[sha], nr):
                return sha, weight
    if find_all or not order:
        return None, weight
    best, best_distance = order[0], -1
    for sha in order:
        distance = min(weight[sha], nr - weight[sha])
        if distance > best_distance:
            best, best_distance = sha, distance
    return best, weight


def get_prn(count: int) -> int:
    value = (count * 1103515245 + 12345) & 0xFFFFFFFF
    return (value // 65536) % PRN_MODULO


def f32(value: float) -> float:
    return struct.unpack("f", struct.pack("f", value))[0]


def sqrti(value: int) -> int:
    if not value:
        return 0
    x = f32(value)
    while True:
        y = f32(f32(x + f32(value / x)) / 2)
        delta = f32(y - x if y > x else x - y)
        x = y
        if delta < 0.5:
            return int(x)


def skip_away(filtered: Sequence[str], bad: str) -> str:
    count = len(filtered)
    prn = get_prn(count)
    index = (count * prn // PRN_MODULO) * sqrti(prn) // sqrti(PRN_MODULO)
    if index >= count:
        return filtered[0]
    if filtered[index] != bad:
        return filtered[index]
    return filtered[index - 1] if index > 0 else filtered[0]


def managed_skipped(ranked: Sequence[str], skipped: Set[str], bad: str) -> Tuple[Optional[str], List[str]]:
    tried: List[str] = []
    filtered: List[str] = []
    for sha in ranked:
        if sha in skipped:
            tried.append(sha)
        elif not tried:
            return sha, []
        else:
            filtered.append(sha)
    if not filtered:
        return None, tried
    return skip_away(filtered, bad), tried


def estimate_steps(candidates: int) -> int:
    if candidates < 3:
        return 0
    n = candidates.bit_length() - 1
    return n if (1 << n) < 3 * (candidates - (1 << n)) else n - 1


def check_merge_bases(commits: Dict[str, Commit], bad: str, goods: Sequence[str], skips: Set[str]) -> Optional[str]:
    reachable = ancestors(commits, [bad])
    for good in goods:
        if good in reachable:
            continue
        for base in merge_bases(commits, bad, good):
            if base == bad:
                raise BisectError(f"the merge base {short(bad)} is bad: the change you are looking for was fixed between {short(bad)} and {short(good)}; swap good and bad")
            if base in goods or base in skips:
                continue
            return base
    return None


def bisect_step(commits: Dict[str, Commit], bad: str, goods: Sequence[str], skips: Iterable[str] = (), verify_bases: bool = False) -> Step:
    for sha in [bad, *goods]:
        if sha not in commits:
            raise BisectError(f"{sha} is not a commit in this history")
    if not goods:
        raise BisectError("bisecting needs at least one good commit")
    skipped = set(skips)
    base = check_merge_bases(commits, bad, goods, skipped) if verify_bases else None
    if base is not None:
        return Step("merge-base", base, candidates=1)
    if bad in ancestors(commits, goods):
        raise BisectError(f"{short(bad)} was both good and bad")
    walked = date_order(commits, [bad, *goods])
    excluded = ancestors(commits, goods)
    order = [sha for sha in walked if sha not in excluded][::-1]
    nr = len(order)
    best, weight = find_bisection(commits, order, bool(skipped))
    full = weight if skipped else find_bisection(commits, order, True)[1]
    distances = {sha: min(full[sha], nr - full[sha]) for sha in order}
    if skipped:
        ranked = sorted(order, key=lambda sha: (-distances[sha], sha))
        chosen, tried = managed_skipped(ranked, skipped, bad)
        reaches = weight[ranked[0]]
    else:
        chosen, tried, reaches = best, [], weight[best] if best else 0
    if chosen is None:
        return Step("only-skipped", None, candidates=nr, suspects=tried, distances=distances)
    if chosen == bad:
        if tried:
            return Step("only-skipped", None, candidates=nr, suspects=tried + [bad], distances=distances)
        return Step("found", bad, candidates=nr, reaches=reaches, distances=distances)
    return Step("test", chosen, nr - reaches - 1, estimate_steps(nr), nr, reaches, [], distances)


def verdict_of(code: int) -> str:
    if code < 0 or code >= 128:
        raise BisectError(f"bisect run failed: exit code {code} is < 0 or >= 128")
    return SKIP if code == 125 else GOOD if code == 0 else BAD


def bisect_run(commits: Dict[str, Commit], bad: str, goods: Sequence[str], test: Callable[[str], int], skips: Sequence[str] = ()) -> Tuple[Step, List[Trial]]:
    goods, skipped, log = list(goods), list(skips), []
    first, verify_bases = True, True
    while True:
        step = bisect_step(commits, bad, goods, skipped, verify_bases)
        verify_bases = step.state == "merge-base"
        if step.state in ("found", "only-skipped"):
            return step, log
        commit = step.commit or bad
        code = test(commit)
        if first and code in (126, 127):
            check = test(min(goods))
            if check < 0 or check >= 128:
                raise BisectError("unable to verify the test command on the good revision")
            if check == code:
                raise BisectError(f"bogus exit code {code} for good revision: the command probably could not be run")
            first = False
        verdict = verdict_of(code)
        log.append(Trial(commit, code, verdict))
        if verdict == GOOD:
            goods.append(commit)
        elif verdict == BAD:
            bad = commit
        else:
            skipped.append(commit)


def fingerprint(line: str) -> Fingerprint:
    data = line.encode("utf-8", "surrogateescape")
    counts: Fingerprint = {}
    previous = 0
    for position in range(len(data) + 1):
        byte = 0 if position == len(data) or data[position] in GIT_SPACE else data[position]
        current = byte + 32 if 65 <= byte <= 90 else byte
        key = previous | (current << 8)
        previous = current
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def similarity(one: Fingerprint, two: Fingerprint) -> int:
    return sum(min(count, one[key]) for key, count in two.items() if key in one)


def subtract(one: Fingerprint, two: Fingerprint) -> None:
    for key, count in two.items():
        if key in one:
            if one[key] <= count:
                del one[key]
            else:
                one[key] -= count


class FuzzyMatcher:
    def __init__(self, parent: List[Fingerprint], target: List[Fingerprint], tlno: int, parent_slno: int, same: int, parent_len: int) -> None:
        self.parent, self.target = parent, target
        self.mapping = (parent_slno, parent_len, tlno, same - tlno)
        self.b0 = tlno
        length_b = same - tlno
        self.max_a = MAX_SEARCH_DISTANCE if MAX_SEARCH_DISTANCE < parent_len else parent_len - 1
        self.max_b = ((2 * self.max_a + 1) * length_b - 1) // parent_len
        self.width = 2 * self.max_a + 1
        self.result = [-1] * length_b
        self.second = [-1] * length_b
        self.certainty = [CERTAINTY_NOT_CALCULATED] * length_b
        self.similarities = [-1] * (length_b * self.width)

    def closest(self, line_b: int) -> int:
        start_a, length_a, start_b, length_b = self.mapping
        return ((line_b - start_b) * 2 + 1) * length_a // (length_b * 2) + start_a

    def best_match(self, start_a: int, length_a: int, start_b: int, local_b: int) -> None:
        row = start_b - self.b0 + local_b
        if self.certainty[row] != CERTAINTY_NOT_CALCULATED:
            return
        closest = self.closest(start_b + local_b) - start_a
        best = second = best_index = second_index = 0
        for i in range(max(0, closest - self.max_a), min(length_a, closest + self.max_a + 1)):
            slot = row * self.width + i - closest + self.max_a
            if self.similarities[slot] == -1:
                self.similarities[slot] = similarity(self.target[start_b + local_b], self.parent[start_a + i]) * (1000 - abs(i - closest))
            value = self.similarities[slot]
            if value > best:
                second, second_index, best, best_index = best, best_index, value, i
            elif value > second:
                second, second_index = value, i
        if best == 0:
            self.certainty[row], self.result[row] = CERTAIN_NOTHING_MATCHES, -1
        else:
            self.certainty[row] = best * 2 - second
            self.result[row], self.second[row] = start_a + best_index, start_a + second_index

    def match(self, start_a: int, start_b: int, length_a: int, length_b: int) -> None:
        certain_b, certainty = -1, -1
        for i in range(length_b):
            self.best_match(start_a, length_a, start_b, i)
            if self.certainty[start_b - self.b0 + i] > certainty:
                certain_b, certainty = i, self.certainty[start_b - self.b0 + i]
        if certain_b == -1:
            return
        certain_a = self.result[start_b - self.b0 + certain_b]
        subtract(self.parent[certain_a], self.target[start_b + certain_b])
        low, high = max(0, certain_b - self.max_b), min(length_b, certain_b + self.max_b + 1)
        for i in range(low, high):
            closest = self.closest(start_b + i) - start_a
            if abs(certain_a - start_a - closest) <= self.max_a:
                self.similarities[(start_b - self.b0 + i) * self.width + certain_a - start_a - closest + self.max_a] = -1
        for i in range(certain_b - 1, low - 1, -1):
            row = start_b - self.b0 + i
            if self.certainty[row] >= 0 and (self.result[row] >= certain_a or self.second[row] >= certain_a):
                self.certainty[row] = CERTAINTY_NOT_CALCULATED
        for i in range(certain_b + 1, high):
            row = start_b - self.b0 + i
            if self.certainty[row] >= 0 and (self.result[row] <= certain_a or self.second[row] <= certain_a):
                self.certainty[row] = CERTAINTY_NOT_CALCULATED
        if certain_b > 0:
            self.match(start_a, start_b, certain_a + 1 - start_a, certain_b)
        if certain_b + 1 < length_b:
            self.match(certain_a, start_b + certain_b + 1, length_a + start_a - certain_a, length_b - certain_b - 1)


def scan_parent_range(parent: List[Fingerprint], target: List[Fingerprint], t_idx: int) -> int:
    best_value, best_index = FINGERPRINT_FILE_THRESHOLD, -1
    for p_idx in range(len(parent)):
        value = similarity(target[t_idx], parent[p_idx])
        if value < best_value:
            continue
        if value == best_value and best_index != -1 and abs(best_index - t_idx) < abs(p_idx - t_idx):
            continue
        best_value, best_index = value, p_idx
    return best_index


def guess_line_blames(parent: List[Fingerprint], target: List[Fingerprint], tlno: int, offset: int, same: int, parent_len: int) -> List[Tuple[bool, int]]:
    fuzzy: Optional[List[int]] = None
    if parent_len > 0:
        matcher = FuzzyMatcher(parent, target, tlno, tlno + offset, same, parent_len)
        matcher.match(tlno + offset, tlno, parent_len, same - tlno)
        fuzzy = matcher.result
    guesses = []
    for i in range(same - tlno):
        best = fuzzy[i] if fuzzy is not None and fuzzy[i] >= 0 else scan_parent_range(parent, target, tlno + i)
        guesses.append((True, best) if best >= 0 else (False, tlno + i))
    return guesses


def split_at(entry: Entry, length: int) -> Entry:
    rest = Entry(entry.lno + length, entry.count - length, entry.suspect, entry.s_lno + length, entry.ignored, entry.unblamable)
    entry.count = length
    return rest


class Blame:
    def __init__(self, history: History, rev: str, path: str, ignore: Iterable[str] = (), moves: bool = False) -> None:
        self.history, self.path, self.ignore, self.moves = history, path, set(ignore), moves
        self.rev = rev
        blob = history.blob_id(rev, path)
        if blob is None:
            raise BisectError(f"no such path {path} in {short(rev)}")
        self.final = history.lines(blob)
        self.suspects: Dict[str, List[Entry]] = {}
        self.previous: Dict[str, str] = {}
        self.queue: List[Tuple[int, int, str]] = []
        self.pushed = 0
        self.done: List[Entry] = []
        if self.final:
            self.give(rev, [Entry(0, len(self.final), rev, 0)])

    def give(self, sha: str, entries: List[Entry]) -> None:
        if not entries:
            return
        for entry in entries:
            entry.suspect = sha
        if self.suspects.get(sha):
            self.suspects[sha] = sorted(self.suspects[sha] + entries, key=lambda e: e.s_lno)
            return
        self.suspects[sha] = sorted(entries, key=lambda e: e.s_lno)
        self.pushed += 1
        heapq.heappush(self.queue, (-self.history.commit(sha).date, self.pushed, sha))

    def lines_of(self, sha: str) -> List[str]:
        blob = self.history.blob_id(sha, self.path)
        return self.history.lines(blob) if blob else []

    def score(self, entry: Entry) -> int:
        return 1 + sum(1 for line in self.final[entry.lno:entry.lno + entry.count] for byte in line.encode("utf-8", "surrogateescape") if 48 <= byte <= 57 or 65 <= byte <= 90 or 97 <= byte <= 122)

    def run(self) -> List[BlameLine]:
        while self.queue:
            _, _, sha = heapq.heappop(self.queue)
            entries = self.suspects.pop(sha, [])
            if entries:
                self.done.extend(self.pass_blame(sha, entries))
        lines = []
        for entry in sorted(self.done, key=lambda e: e.lno):
            commit = self.history.commit(entry.suspect)
            for k in range(entry.count):
                lines.append(BlameLine(entry.lno + k + 1, entry.suspect, entry.s_lno + k + 1, self.final[entry.lno + k], not commit.parents, entry.ignored, entry.unblamable, self.previous.get(entry.suspect)))
        return lines

    def pass_blame(self, sha: str, entries: List[Entry]) -> List[Entry]:
        commit = self.history.commit(sha)
        blob = self.history.blob_id(sha, self.path)
        origins: List[Optional[str]] = []
        seen: List[str] = []
        for parent in commit.parents:
            parent_blob = self.history.blob_id(parent, self.path)
            if parent_blob is not None and parent_blob == blob:
                self.give(parent, entries)
                return []
            fresh = parent_blob is not None and parent_blob not in seen
            origins.append(parent if fresh else None)
            seen.extend([parent_blob] if fresh and parent_blob else [])
        for parent in origins:
            if parent is None:
                continue
            self.previous.setdefault(sha, parent)
            entries = self.pass_to_parent(sha, parent, entries, False)
            if not entries:
                return []
        if sha in self.ignore:
            for parent in origins:
                if parent is not None:
                    entries = self.pass_to_parent(sha, parent, entries, True)
                    if not entries:
                        return []
        if self.moves:
            small = [e for e in entries if self.score(e) <= MOVE_SCORE]
            entries = [e for e in entries if self.score(e) > MOVE_SCORE]
            blamed: List[Entry] = []
            for parent in origins:
                if parent is not None and entries:
                    entries = self.find_move(parent, entries, blamed, small)
            for parent in dict.fromkeys(e.suspect for e in blamed):
                self.give(parent, [e for e in blamed if e.suspect == parent])
            entries = small + entries
        return entries

    def pass_to_parent(self, sha: str, parent: str, entries: List[Entry], ignore: bool) -> List[Entry]:
        parent_lines, target_lines = self.lines_of(parent), self.lines_of(sha)
        fingerprints = ([fingerprint(line) for line in parent_lines], [fingerprint(line) for line in target_lines]) if ignore else None
        moved: List[Entry] = []
        kept: List[Entry] = []
        pending = list(entries)
        offset = 0
        for change in diff(parent_lines, target_lines):
            pending = self.chunk(pending, kept, moved, change.new_start, change.old_start - change.new_start, change.new_start + change.new_count, change.old_count, fingerprints)
            offset = change.old_start + change.old_count - change.new_start - change.new_count
        self.chunk(pending, kept, moved, LINE_MAX, offset, LINE_MAX, 0, None)
        for entry in moved:
            entry.suspect = parent
        self.give(parent, moved)
        return kept

    def chunk(self, pending: List[Entry], kept: List[Entry], moved: List[Entry], tlno: int, offset: int, same: int, parent_len: int, fingerprints: Optional[Tuple[List[Fingerprint], List[Fingerprint]]]) -> List[Entry]:
        rest: List[Entry] = []
        while pending and pending[0].s_lno < tlno:
            entry = pending.pop(0)
            if entry.s_lno + entry.count > tlno:
                rest.append(split_at(entry, tlno - entry.s_lno))
            entry.s_lno += offset
            moved.append(entry)
        pending = rest + pending
        guesses = guess_line_blames(fingerprints[0], fingerprints[1], tlno, offset, same, parent_len) if fingerprints and same > tlno else []
        after: List[Entry] = []
        while pending and pending[0].s_lno < same:
            entry = pending.pop(0)
            if entry.s_lno + entry.count > same:
                after.append(split_at(entry, same - entry.s_lno))
            if fingerprints:
                self.ignore_entry(entry, kept, moved, guesses[entry.s_lno - tlno:])
            else:
                kept.append(entry)
        return after + pending

    def ignore_entry(self, entry: Entry, kept: List[Entry], moved: List[Entry], guesses: Sequence[Tuple[bool, int]]) -> None:
        start = 0
        for i in range(entry.count):
            if i + 1 < entry.count and guesses[i][0] == guesses[i + 1][0] and guesses[i][1] + 1 == guesses[i + 1][1]:
                continue
            piece = Entry(entry.lno + start, i + 1 - start, entry.suspect, entry.s_lno + start, entry.ignored, entry.unblamable)
            if guesses[i][0]:
                piece.ignored, piece.s_lno = True, guesses[start][1]
                moved.append(piece)
            else:
                piece.unblamable = True
                kept.append(piece)
            start = i + 1

    def find_move(self, parent: str, entries: List[Entry], blamed: List[Entry], small: List[Entry]) -> List[Entry]:
        parent_lines = self.lines_of(parent)
        leftover: List[Entry] = []
        unblamed = entries
        while unblamed:
            pieces: List[Entry] = []
            for entry in unblamed:
                split = self.find_copy(entry, parent, parent_lines)
                if split[1] is not None and MOVE_SCORE < self.score(split[1]):
                    blamed.append(split[1])
                    pieces.extend(piece for piece in (split[0], split[2]) if piece is not None)
                else:
                    leftover.append(entry)
            small.extend(e for e in pieces if self.score(e) <= MOVE_SCORE)
            unblamed = [e for e in pieces if self.score(e) > MOVE_SCORE]
        return leftover

    def find_copy(self, entry: Entry, parent: str, parent_lines: List[str]) -> List[Optional[Entry]]:
        best: List[Optional[Entry]] = [None, None, None]
        tlno = plno = 0

        def consider(tlno: int, plno: int, same: int) -> None:
            nonlocal best
            if entry.count <= tlno or tlno >= same:
                return
            potential = self.split_overlap(entry, tlno + entry.s_lno, plno, same + entry.s_lno, parent)
            if potential[1] is not None and (best[1] is None or self.score(potential[1]) >= self.score(best[1])):
                best = potential

        for change in diff(parent_lines, self.final[entry.lno:entry.lno + entry.count]):
            consider(tlno, plno, change.new_start)
            plno, tlno = change.old_start + change.old_count, change.new_start + change.new_count
        consider(tlno, plno, entry.count)
        return best

    @staticmethod
    def split_overlap(entry: Entry, tlno: int, plno: int, same: int, parent: str) -> List[Optional[Entry]]:
        before = after = None
        if entry.s_lno < tlno:
            before = Entry(entry.lno, tlno - entry.s_lno, entry.suspect, entry.s_lno, entry.ignored, entry.unblamable)
            middle_lno, middle_s = entry.lno + tlno - entry.s_lno, plno
        else:
            middle_lno, middle_s = entry.lno, plno + entry.s_lno - tlno
        if same < entry.s_lno + entry.count:
            after = Entry(entry.lno + same - entry.s_lno, entry.s_lno + entry.count - same, entry.suspect, same, entry.ignored, entry.unblamable)
            end = after.lno
        else:
            end = entry.lno + entry.count
        if end - middle_lno < 1:
            return [before, None, after]
        return [before, Entry(middle_lno, end - middle_lno, parent, middle_s, entry.ignored, entry.unblamable), after]


def read_ignore_revs(path: str) -> List[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        raise BisectError(f"cannot read {path}: {error.strerror}") from None
    revs = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if not OBJECT_ID.match(line):
            raise BisectError(f"{path}:{number}: {line!r} is not a full object name")
        revs.append(line)
    return revs


def occurrences(text: str, needle: str) -> int:
    return text.count(needle) if needle else 0


def pickaxe(history: History, order: Sequence[str], needle: Optional[str] = None, pattern: Optional[str] = None, paths: Sequence[str] = ()) -> List[Tuple[str, List[str]]]:
    regex = re.compile(pattern) if pattern is not None else None
    found = []
    for sha in order:
        commit = history.commit(sha)
        if len(commit.parents) > 1:
            continue
        before = history.tree(commit.parents[0]) if commit.parents else {}
        after = history.tree(sha)
        hits = []
        for path in sorted(set(before) | set(after)):
            if before.get(path) == after.get(path) or (paths and not any(path == p or path.startswith(p.rstrip("/") + "/") for p in paths)):
                continue
            old = history.blob(before[path]) if path in before else ""
            new = history.blob(after[path]) if path in after else ""
            if regex is None:
                if occurrences(old, needle or "") != occurrences(new, needle or ""):
                    hits.append(path)
                continue
            old_lines, new_lines = split_lines(old), split_lines(new)
            changes = diff(old_lines, new_lines)
            touched = [line for c in changes for line in old_lines[c.old_start:c.old_start + c.old_count] + new_lines[c.new_start:c.new_start + c.new_count]]
            if any(regex.search(line) for line in touched):
                hits.append(path)
        if hits:
            found.append((sha, hits))
    return found


def parse_reflog(ref: str, text: str) -> List[ReflogEntry]:
    entries = []
    lines = [line for line in text.splitlines() if line.strip()]
    for position, line in enumerate(lines):
        head, _, message = line.partition("\t")
        parts = head.split(" ", 2)
        if len(parts) < 3 or not OBJECT_ID.match(parts[0]) or not OBJECT_ID.match(parts[1]):
            continue
        entries.append(ReflogEntry(ref, len(lines) - 1 - position, parts[0], parts[1], message))
    return entries


def read_reflogs(git_dir: str, common_dir: str) -> List[ReflogEntry]:
    entries: List[ReflogEntry] = []
    head = os.path.join(git_dir, "logs", "HEAD")
    if os.path.isfile(head):
        with open(head, encoding="utf-8", errors="surrogateescape") as handle:
            entries.extend(parse_reflog("HEAD", handle.read()))
    refs = os.path.join(common_dir, "logs", "refs")
    for folder, _, names in sorted(os.walk(refs)):
        for name in sorted(names):
            path = os.path.join(folder, name)
            ref = os.path.relpath(path, os.path.join(common_dir, "logs")).replace(os.sep, "/")
            with open(path, encoding="utf-8", errors="surrogateescape") as handle:
                entries.extend(parse_reflog(ref, handle.read()))
    return entries


def lost_commits(commits: Dict[str, Commit], roots: Iterable[str], reflog: Sequence[ReflogEntry]) -> List[Dict[str, object]]:
    reachable = ancestors(commits, roots)
    unreachable = set(commits) - reachable
    has_child = {parent for sha in unreachable for parent in commits[sha].parents}
    tips = sorted((sha for sha in unreachable if sha not in has_child), key=lambda sha: (-commits[sha].date, sha))
    from_reflog = ancestors(commits, [sha for entry in reflog for sha in (entry.old, entry.new)])
    found = []
    for sha in tips:
        mentions = [f"{entry.name}: {entry.message}" for entry in reflog if sha in (entry.old, entry.new)]
        found.append({"commit": sha, "subject": commits[sha].subject, "date": commits[sha].date, "reflog": mentions, "dangling": sha not in from_reflog})
    return found


def repo_lost(runner: Runner, repo: str) -> List[Dict[str, object]]:
    listing = git(runner, repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)")
    shas = [line.split()[0] for line in listing.splitlines() if line.endswith(" commit")]
    history = GitHistory(runner, repo, shas=shas) if shas else History({})
    roots = []
    for line in git(runner, repo, "for-each-ref", "--format=%(objectname) %(*objectname)").splitlines():
        roots.extend(line.split())
    code, head, _ = runner(["git", "-C", repo, "rev-parse", "--verify", "--quiet", "HEAD"])
    if code == 0:
        roots.append(head.strip())
    git_dir, common_dir = git(runner, repo, "rev-parse", "--absolute-git-dir", "--git-common-dir").splitlines()[:2]
    if not os.path.isabs(common_dir):
        common_dir = os.path.normpath(os.path.join(repo, common_dir))
    return lost_commits(history.commits, roots, read_reflogs(git_dir, common_dir))


def read_graph(path: str) -> Dict[str, Commit]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as error:
        raise BisectError(f"cannot read {path}: {error.strerror}") from None
    except json.JSONDecodeError as error:
        raise BisectError(f"{path} is not valid JSON: line {error.lineno} column {error.colno}: {error.msg}") from None
    commits = data.get("commits") if isinstance(data, dict) else None
    if not isinstance(commits, dict) or not all(isinstance(c, dict) and isinstance(c.get("parents"), list) and isinstance(c.get("date"), int) for c in commits.values()):
        raise BisectError(f'{path} must be {{"commits": {{"NAME": {{"parents": [...], "date": N, "subject": "..."}}}}}}')
    return {name: Commit(name, tuple(c["parents"]), c["date"], c.get("subject", "")) for name, c in commits.items()}


def step_json(step: Step, commits: Dict[str, Commit]) -> Dict[str, object]:
    data = {"state": step.state, "commit": step.commit, "subject": commits[step.commit].subject if step.commit else None, "remaining": step.remaining, "steps": step.steps, "candidates": step.candidates, "reaches": step.reaches, "suspects": step.suspects}
    data["distances"] = dict(sorted(step.distances.items(), key=lambda item: (-item[1], item[0])))
    return data


def describe(step: Step, commits: Dict[str, Commit]) -> str:
    if step.state == "found":
        return f"{step.commit} is the first bad commit\n    {commits[step.commit or ''].subject}\n"
    if step.state == "only-skipped":
        return "There are only 'skip'ped commits left to test.\nThe first bad commit could be any of:\n" + "".join(f"{sha}\n" for sha in step.suspects) + "We cannot bisect more!\n"
    if step.state == "merge-base":
        return f"Bisecting: a merge base must be tested\n[{step.commit}] {commits[step.commit or ''].subject}\n"
    plural = "revision" if step.remaining == 1 else "revisions"
    return f"Bisecting: {step.remaining} {plural} left to test after this (roughly {step.steps} step{'' if step.steps == 1 else 's'})\n[{step.commit}] {commits[step.commit or ''].subject}\n"


def load_bisect(args: argparse.Namespace, runner: Runner) -> Tuple[Dict[str, Commit], str, List[str], List[str]]:
    if args.graph:
        commits = read_graph(args.graph)
        return commits, args.bad, list(args.good), list(args.skip)
    bad = resolve(runner, args.repo, args.bad)
    goods = [resolve(runner, args.repo, g) for g in args.good]
    skips = [resolve(runner, args.repo, s) for s in args.skip]
    return GitHistory(runner, args.repo, [bad, *goods]).commits, bad, goods, skips


def run_next(args: argparse.Namespace, runner: Runner) -> int:
    commits, bad, goods, skips = load_bisect(args, runner)
    step = bisect_step(commits, bad, goods, skips, args.verify_bases)
    if args.json:
        print(json.dumps(step_json(step, commits), indent=2))
    elif args.all:
        for sha, distance in sorted(step.distances.items(), key=lambda item: (-item[1], item[0])):
            print(f"{sha} (dist={distance})")
    else:
        sys.stdout.write(describe(step, commits))
    return 0


def checkout_tester(runner: Runner, repo: str, command: Sequence[str], verbose: bool) -> Callable[[str], int]:
    def test(sha: str) -> int:
        git(runner, repo, "checkout", "-q", "--detach", sha)
        code, _, _ = runner(list(command), cwd=repo)
        if verbose:
            print(f"minibisect: {' '.join(command)} at {short(sha)} exited {code}", file=sys.stderr)
        return code
    return test


def run_bisect(args: argparse.Namespace, runner: Runner) -> int:
    if not args.command:
        raise BisectError("run needs a test command after --")
    commits, bad, goods, skips = load_bisect(args, runner)
    if git(runner, args.repo, "status", "--porcelain", "--untracked-files=no").strip():
        raise BisectError(f"{args.repo} has uncommitted changes; commit or stash them first")
    code, branch, _ = runner(["git", "-C", args.repo, "symbolic-ref", "-q", "--short", "HEAD"])
    original = branch.strip() if code == 0 and branch.strip() else git(runner, args.repo, "rev-parse", "HEAD").strip()
    try:
        step, log = bisect_run(commits, bad, goods, checkout_tester(runner, args.repo, args.command, not args.json), skips)
    finally:
        git(runner, args.repo, "checkout", "-q", original)
    if args.json:
        data = step_json(step, commits)
        data["log"] = [{"commit": t.commit, "exit": t.exit, "verdict": t.verdict} for t in log]
        print(json.dumps(data, indent=2))
    else:
        for trial in log:
            print(f"# {trial.verdict}: [{trial.commit}] {commits[trial.commit].subject}")
        sys.stdout.write(describe(step, commits))
    return 0 if step.state == "found" else 1


def blame_json(lines: Sequence[BlameLine], history: History, path: str, rev: str) -> Dict[str, object]:
    return {"path": path, "rev": rev, "lines": [{"line": b.line, "commit": b.commit, "orig_line": b.orig_line, "author": history.commit(b.commit).author, "summary": history.commit(b.commit).subject, "boundary": b.boundary, "ignored": b.ignored, "unblamable": b.unblamable, "previous": b.previous, "text": b.text} for b in lines]}


def line_porcelain(lines: Sequence[BlameLine], history: History, path: str) -> str:
    out = []
    for b in lines:
        c = history.commit(b.commit)
        out.append(f"{b.commit} {b.orig_line} {b.line}\n")
        out.append(f"author {c.author}\nauthor-mail {c.author_mail}\nauthor-time {c.author_date.split()[0]}\nauthor-tz {c.author_date.split()[-1]}\n")
        out.append(f"committer {c.committer}\ncommitter-mail {c.committer_mail}\ncommitter-time {c.committer_date.split()[0]}\ncommitter-tz {c.committer_date.split()[-1]}\n")
        out.append(f"summary {c.subject}\n" + ("boundary\n" if b.boundary else ""))
        if b.previous:
            out.append(f"previous {b.previous} {path}\n")
        out.append(f"filename {path}\n" + ("ignored\n" if b.ignored else "") + ("unblamable\n" if b.unblamable else ""))
        out.append("\t" + b.text + ("" if b.text.endswith("\n") else "\n"))
    return "".join(out)


def run_blame(args: argparse.Namespace, runner: Runner) -> int:
    rev = resolve(runner, args.repo, args.rev)
    history = GitHistory(runner, args.repo, [rev])
    ignore = [resolve(runner, args.repo, r) for r in args.ignore_rev]
    for path in args.ignore_revs_file:
        ignore.extend(read_ignore_revs(path))
    lines = Blame(history, rev, args.path, ignore, args.moves).run()
    if args.json:
        print(json.dumps(blame_json(lines, history, args.path, rev), indent=2))
    elif args.line_porcelain:
        sys.stdout.write(line_porcelain(lines, history, args.path))
    else:
        width = max([len(history.commit(b.commit).author) for b in lines] or [1])
        for b in lines:
            mark = "^" if b.boundary else "*" if b.unblamable else "?" if b.ignored else " "
            sys.stdout.write(f"{mark}{b.commit[:8]} ({history.commit(b.commit).author:<{width}} {b.line:>4}) {b.text.rstrip(chr(10))}\n")
    return 0


def run_pickaxe(args: argparse.Namespace, runner: Runner) -> int:
    if (args.S is None) == (args.G is None):
        raise BisectError("give exactly one of -S STRING or -G REGEX")
    if args.G is not None:
        try:
            re.compile(args.G)
        except re.error as error:
            raise BisectError(f"invalid regular expression {args.G!r}: {error}") from None
    rev = resolve(runner, args.repo, args.rev)
    history = GitHistory(runner, args.repo, [rev])
    found = pickaxe(history, date_order(history.commits, [rev]), args.S, args.G, args.paths)
    if args.json:
        print(json.dumps([{"commit": sha, "subject": history.commit(sha).subject, "paths": paths} for sha, paths in found], indent=2))
    else:
        for sha, paths in found:
            print(f"{sha} {history.commit(sha).subject} ({', '.join(paths)})")
    return 0 if found else 1


def run_lost(args: argparse.Namespace, runner: Runner) -> int:
    found = repo_lost(runner, args.repo)
    if args.json:
        print(json.dumps(found, indent=2))
        return 0
    for item in found:
        dangling = "dangling" if item["dangling"] else "reflog only"
        print(f"{item['commit']} {dangling}: {item['subject']}")
        for mention in item["reflog"]:
            print(f"    {mention}")
    if found:
        print(f"recover with: git branch rescued {found[0]['commit']}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minibisect", description="Bisection over a commit graph, line blame, pickaxe search and lost-commit recovery.")
    sub = parser.add_subparsers(dest="command_name", required=True)
    for name, text in (("next", "print the next commit to test (or the first bad commit)"), ("run", "check out and test commits until the first bad one is found")):
        p = sub.add_parser(name, help=text)
        source = p.add_mutually_exclusive_group()
        source.add_argument("--repo", default=".", help="repository to read (default: .)")
        source.add_argument("--graph", help='JSON file {"commits": {"NAME": {"parents": [...], "date": N}}} instead of a repository')
        p.add_argument("--bad", required=True, help="a commit that has the bug")
        p.add_argument("--good", action="append", default=[], required=True, help="a commit without the bug (repeatable)")
        p.add_argument("--skip", action="append", default=[], help="an untestable commit (repeatable)")
        p.add_argument("--json", action="store_true", help="machine-readable output")
        if name == "next":
            p.add_argument("--all", action="store_true", help="print every candidate with its distance, like git rev-list --bisect-all")
            p.add_argument("--verify-bases", action="store_true", help="first test merge bases of bad and goods that are not its ancestors, as git bisect start does")
        else:
            p.add_argument("command", nargs=argparse.REMAINDER, help="-- test command: exit 0 good, 125 skip, 1-127 bad, other aborts")
    p = sub.add_parser("blame", help="blame each line of a file on the commit that last changed it")
    p.add_argument("--repo", default=".")
    p.add_argument("-M", dest="moves", action="store_true", help="also follow lines moved within the file")
    p.add_argument("--ignore-rev", action="append", default=[], help="a commit whose changes are passed through (repeatable)")
    p.add_argument("--ignore-revs-file", action="append", default=[], help="file of full commit names to ignore, # comments allowed")
    p.add_argument("--json", action="store_true")
    p.add_argument("--line-porcelain", action="store_true", help="git blame --line-porcelain layout")
    p.add_argument("rev", nargs="?", default="HEAD")
    p.add_argument("path")
    p = sub.add_parser("pickaxe", help="commits that changed the number of occurrences of a string (-S) or touched lines matching a regex (-G)")
    p.add_argument("--repo", default=".")
    p.add_argument("-S", help="string whose number of occurrences changed")
    p.add_argument("-G", help="regular expression matched against added and removed lines")
    p.add_argument("--json", action="store_true")
    p.add_argument("rev", nargs="?", default="HEAD")
    p.add_argument("paths", nargs="*", help="limit to these paths")
    p = sub.add_parser("lost", help="unreachable commits: reflog entries and dangling objects")
    p.add_argument("--repo", default=".")
    p.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None, runner: Runner = subprocess_runner) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if args.command_name == "run":
        args.command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        if args.command_name == "next":
            return run_next(args, runner)
        if args.command_name == "run":
            if args.graph:
                raise BisectError("run needs a repository to check out, not --graph")
            return run_bisect(args, runner)
        if args.command_name == "blame":
            return run_blame(args, runner)
        if args.command_name == "pickaxe":
            return run_pickaxe(args, runner)
        return run_lost(args, runner)
    except BisectError as error:
        print(f"minibisect: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
