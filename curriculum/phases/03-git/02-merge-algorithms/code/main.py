"""minimerge: Myers, patience and histogram diffs, unified diffs, merge bases in a commit graph, three-way merges with merge, diff3 and zdiff3 conflicts, virtual merge bases and rename detection."""
from __future__ import annotations

import argparse
import heapq
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

Runner = Callable[[Sequence[str]], Tuple[int, str, str]]
Graph = Dict[str, Tuple[str, ...]]
Marks = List[bool]

ALGORITHMS = ("myers", "patience", "histogram")
STYLES = ("merge", "diff3", "zdiff3")
LEVELS = ("minimal", "eager", "zealous", "zealous-alnum")
FAVORS = {"": 0, "ours": 1, "theirs": 2, "union": 3}
CONFLICT, OURS, THEIRS, UNION, SAME = 0, 1, 2, 3, 4
MODE_NAMES = {CONFLICT: "conflict", OURS: "ours", THEIRS: "theirs", UNION: "union", SAME: "same"}
NO_MATCH, MATCH, MANY = 0, 1, 2
MAX_EQLIMIT = 1024
SIMSCAN_WINDOW = 100
KPDIS_RUN = 4
MAX_CHAIN = 64
LINE_MAX = sys.maxsize
NON_UNIQUE = -1
MARKER_SIZE = 7
MAX_SCORE = 60000
HASHBASE = 107927
MASK = 0xFFFFFFFF
CANDIDATES_PER_TARGET = 4
PARENT1, PARENT2, STALE, RESULT = 1, 2, 4, 8
VIRTUAL_NAMES = ("Temporary merge branch 1", "Temporary merge branch 2")


class MergeError(Exception):
    pass


@dataclass(frozen=True)
class Change:
    old_start: int
    old_count: int
    new_start: int
    new_count: int


@dataclass
class Region:
    mode: int
    base_start: int
    base_count: int
    ours_start: int
    ours_count: int
    theirs_start: int
    theirs_count: int


@dataclass
class MergeResult:
    lines: List[str]
    conflicts: int
    regions: List[Region]


@dataclass(frozen=True)
class Rename:
    source: str
    target: str
    score: int

    @property
    def similarity(self) -> int:
        return self.score * 100 // MAX_SCORE


def split_lines(text: str) -> List[str]:
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as handle:
            return handle.read()
    except OSError as error:
        raise MergeError(f"cannot read {path}: {error.strerror}") from None


def write_text(path: str, text: str) -> None:
    try:
        with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as handle:
            handle.write(text)
    except OSError as error:
        raise MergeError(f"cannot write {path}: {error.strerror}") from None


def classify(*sequences: Sequence[str]) -> List[List[int]]:
    ids: Dict[str, int] = {}
    return [[ids.setdefault(line, len(ids)) for line in sequence] for sequence in sequences]


def bogosqrt(n: int) -> int:
    root = 1
    while n > 0:
        root <<= 1
        n >>= 2
    return root


def mark(changed: Marks, start: int, end: int) -> None:
    for i in range(start, end):
        changed[i] = True


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


def classic_range(a: Sequence[int], b: Sequence[int], start1: int, end1: int, start2: int, end2: int, changed_a: Marks, changed_b: Marks) -> None:
    sub_a, sub_b = myers(a[start1:end1], b[start2:end2])
    changed_a[start1:end1] = sub_a
    changed_b[start2:end2] = sub_b


def unique_lcs(pairs: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    tails: List[int] = []
    previous: List[int] = []
    for index, (_, line2) in enumerate(pairs):
        low, high = -1, len(tails)
        while low + 1 < high:
            middle = low + (high - low) // 2
            if pairs[tails[middle]][1] > line2:
                high = middle
            else:
                low = middle
        previous.append(tails[low] if low >= 0 else -1)
        if low + 1 == len(tails):
            tails.append(index)
        else:
            tails[low + 1] = index
    chain = []
    k = tails[-1] if tails else -1
    while k >= 0:
        chain.append(pairs[k])
        k = previous[k]
    return chain[::-1]


def patience(a: Sequence[int], b: Sequence[int]) -> Tuple[Marks, Marks]:
    changed_a, changed_b = [False] * len(a), [False] * len(b)
    patience_range(a, b, 0, len(a), 0, len(b), changed_a, changed_b)
    return changed_a, changed_b


def patience_range(a: Sequence[int], b: Sequence[int], start1: int, end1: int, start2: int, end2: int, changed_a: Marks, changed_b: Marks) -> None:
    if start1 == end1 or start2 == end2:
        mark(changed_a, start1, end1)
        mark(changed_b, start2, end2)
        return
    first: Dict[int, int] = {}
    partner: Dict[int, int] = {}
    for i in range(start1, end1):
        if a[i] in first:
            partner[a[i]] = NON_UNIQUE
        else:
            first[a[i]] = i
    matched = False
    for j in range(start2, end2):
        if b[j] in first:
            matched = True
            partner[b[j]] = NON_UNIQUE if b[j] in partner else j
    if not matched:
        mark(changed_a, start1, end1)
        mark(changed_b, start2, end2)
        return
    anchors = unique_lcs([(line1, partner[value]) for value, line1 in first.items() if partner.get(value, NON_UNIQUE) != NON_UNIQUE])
    if not anchors:
        classic_range(a, b, start1, end1, start2, end2, changed_a, changed_b)
        return
    line1, line2, k = start1, start2, 0
    while True:
        if k < len(anchors):
            next1, next2 = anchors[k]
            while next1 > line1 and next2 > line2 and a[next1 - 1] == b[next2 - 1]:
                next1 -= 1
                next2 -= 1
        else:
            next1, next2 = end1, end2
        while line1 < next1 and line2 < next2 and a[line1] == b[line2]:
            line1 += 1
            line2 += 1
        if next1 > line1 or next2 > line2:
            patience_range(a, b, line1, next1, line2, next2, changed_a, changed_b)
        if k == len(anchors):
            return
        while k + 1 < len(anchors) and anchors[k + 1] == (anchors[k][0] + 1, anchors[k][1] + 1):
            k += 1
        line1, line2 = anchors[k][0] + 1, anchors[k][1] + 1
        k += 1


def rarest_common_region(a: Sequence[int], b: Sequence[int], start1: int, end1: int, start2: int, end2: int) -> Tuple[str, Optional[Tuple[int, int, int, int]]]:
    occurrences: Dict[int, List[int]] = {}
    for i in range(start1, end1):
        occurrences.setdefault(a[i], []).append(i)
    best: Optional[Tuple[int, int, int, int]] = None
    best_length, best_count = 0, MAX_CHAIN + 1
    has_common = False
    last1, last2 = end1 - 1, end2 - 1
    b_ptr = start2
    while b_ptr <= last2:
        b_next = b_ptr + 1
        places = occurrences.get(b[b_ptr])
        if places is not None:
            has_common = True
            k = 0 if len(places) <= best_count else len(places)
            while k < len(places):
                begin1 = stop1 = places[k]
                begin2 = stop2 = b_ptr
                rarity = len(places)
                while begin1 > start1 and begin2 > start2 and a[begin1 - 1] == b[begin2 - 1]:
                    begin1 -= 1
                    begin2 -= 1
                    if rarity > 1:
                        rarity = min(rarity, len(occurrences[a[begin1]]))
                while stop1 < last1 and stop2 < last2 and a[stop1 + 1] == b[stop2 + 1]:
                    stop1 += 1
                    stop2 += 1
                    if rarity > 1:
                        rarity = min(rarity, len(occurrences[a[stop1]]))
                if b_next <= stop2:
                    b_next = stop2 + 1
                if best_length < stop1 - begin1 or rarity < best_count:
                    best = (begin1, begin2, stop1, stop2)
                    best_length, best_count = stop1 - begin1, rarity
                k += 1
                while k < len(places) and places[k] <= stop1:
                    k += 1
        b_ptr = b_next
    if has_common and best_count > MAX_CHAIN:
        return "fallback", None
    if best is None:
        return "none", None
    return "region", best


def histogram(a: Sequence[int], b: Sequence[int]) -> Tuple[Marks, Marks]:
    changed_a, changed_b = [False] * len(a), [False] * len(b)
    histogram_range(a, b, 0, len(a), 0, len(b), changed_a, changed_b)
    return changed_a, changed_b


def histogram_range(a: Sequence[int], b: Sequence[int], start1: int, end1: int, start2: int, end2: int, changed_a: Marks, changed_b: Marks) -> None:
    while True:
        if start1 >= end1 or start2 >= end2:
            mark(changed_a, start1, end1)
            mark(changed_b, start2, end2)
            return
        status, region = rarest_common_region(a, b, start1, end1, start2, end2)
        if status == "fallback":
            classic_range(a, b, start1, end1, start2, end2, changed_a, changed_b)
            return
        if region is None:
            mark(changed_a, start1, end1)
            mark(changed_b, start2, end2)
            return
        begin1, begin2, stop1, stop2 = region
        histogram_range(a, b, start1, begin1, start2, begin2, changed_a, changed_b)
        start1, start2 = stop1 + 1, stop2 + 1


ALGORITHM = {"myers": myers, "patience": patience, "histogram": histogram}


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


def compact(lines: Sequence[int], changed: Marks, other_lines: Sequence[int], other_changed: Marks, rediff: bool = False) -> None:
    group, other = Group(changed), Group(other_changed)
    while True:
        if not group.is_empty():
            original = (group.start, group.end)
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
            if rediff and not other.is_empty() and original != (group.start, group.end):
                classic_range(lines, other_lines, group.start, group.end, other.start, other.end, changed, other_changed)
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


def diff(old: Sequence[str], new: Sequence[str], algorithm: str = "myers", rediff: bool = True) -> List[Change]:
    if algorithm not in ALGORITHM:
        raise MergeError(f"unknown diff algorithm {algorithm!r} (choose from {', '.join(ALGORITHMS)})")
    a, b = classify(old, new)
    changed_a, changed_b = ALGORITHM[algorithm](a, b)
    rediff = rediff and algorithm == "histogram"
    compact(a, changed_a, b, changed_b, rediff)
    compact(b, changed_b, a, changed_a, rediff)
    return build_script(changed_a, changed_b)


def hunks(changes: Sequence[Change], context: int) -> List[List[Change]]:
    groups: List[List[Change]] = []
    for change in changes:
        if groups and change.old_start - (groups[-1][-1].old_start + groups[-1][-1].old_count) <= 2 * context:
            groups[-1].append(change)
        else:
            groups.append([change])
    return groups


def hunk_range(start: int, count: int) -> str:
    if count == 0:
        return f"{start},0"
    if count == 1:
        return str(start + 1)
    return f"{start + 1},{count}"


def prefixed(prefix: str, lines: Sequence[str]) -> Iterator[str]:
    for line in lines:
        yield prefix + line
        if not line.endswith("\n"):
            yield "\n\\ No newline at end of file\n"


def unified(old: Sequence[str], new: Sequence[str], changes: Sequence[Change], context: int = 3, old_label: str = "a", new_label: str = "b") -> str:
    if not changes:
        return ""
    out = [f"--- {old_label}\n", f"+++ {new_label}\n"]
    for group in hunks(changes, context):
        first, last = group[0], group[-1]
        old_from = max(0, first.old_start - context)
        old_to = min(len(old), last.old_start + last.old_count + context)
        new_from = first.new_start - (first.old_start - old_from)
        new_to = last.new_start + last.new_count + (old_to - last.old_start - last.old_count)
        out.append(f"@@ -{hunk_range(old_from, old_to - old_from)} +{hunk_range(new_from, new_to - new_from)} @@\n")
        position = old_from
        for change in group:
            out.extend(prefixed(" ", old[position:change.old_start]))
            out.extend(prefixed("-", old[change.old_start:change.old_start + change.old_count]))
            out.extend(prefixed("+", new[change.new_start:change.new_start + change.new_count]))
            position = change.old_start + change.old_count
        out.extend(prefixed(" ", old[position:old_to]))
    return "".join(out)


def append_region(regions: List[Region], mode: int, base_start: int, base_count: int, ours_start: int, ours_count: int, theirs_start: int, theirs_count: int) -> None:
    if regions:
        last = regions[-1]
        if ours_start <= last.ours_start + last.ours_count or theirs_start <= last.theirs_start + last.theirs_count:
            if mode != last.mode:
                last.mode = CONFLICT
            last.base_count = base_start + base_count - last.base_start
            last.ours_count = ours_start + ours_count - last.ours_start
            last.theirs_count = theirs_start + theirs_count - last.theirs_start
            return
    regions.append(Region(mode, base_start, base_count, ours_start, ours_count, theirs_start, theirs_count))


def pair_changes(sizes: Tuple[int, int, int], ours: Sequence[str], theirs: Sequence[str], left: Sequence[Change], right: Sequence[Change], level: int) -> List[Region]:
    base_size, ours_size, theirs_size = sizes
    regions: List[Region] = []
    p = q = 0
    while p < len(left) and q < len(right):
        x, y = left[p], right[q]
        if x.old_start + x.old_count < y.old_start:
            append_region(regions, OURS, x.old_start, x.old_count, x.new_start, x.new_count, y.new_start - y.old_start + x.old_start, x.old_count)
            p += 1
            continue
        if y.old_start + y.old_count < x.old_start:
            append_region(regions, THEIRS, y.old_start, y.old_count, x.new_start - x.old_start + y.old_start, y.old_count, y.new_start, y.new_count)
            q += 1
            continue
        same = (x.old_start, x.old_count, x.new_count) == (y.old_start, y.old_count, y.new_count) and ours[x.new_start:x.new_start + x.new_count] == theirs[y.new_start:y.new_start + y.new_count]
        if level == LEVELS.index("minimal") or not same:
            offset = x.old_start - y.old_start
            overhang = offset + x.old_count - y.old_count
            base_start, ours_start, theirs_start = x.old_start, x.new_start, y.new_start
            if offset > 0:
                base_start -= offset
                ours_start -= offset
            else:
                theirs_start += offset
            base_count = x.old_start + x.old_count - base_start
            ours_count = x.new_start + x.new_count - ours_start
            theirs_count = y.new_start + y.new_count - theirs_start
            if overhang < 0:
                base_count -= overhang
                ours_count -= overhang
            else:
                theirs_count += overhang
            append_region(regions, CONFLICT, base_start, base_count, ours_start, ours_count, theirs_start, theirs_count)
        end_x, end_y = x.old_start + x.old_count, y.old_start + y.old_count
        if end_x >= end_y:
            q += 1
        if end_y >= end_x:
            p += 1
    for x in left[p:]:
        append_region(regions, OURS, x.old_start, x.old_count, x.new_start, x.new_count, x.old_start + theirs_size - base_size, x.old_count)
    for y in right[q:]:
        append_region(regions, THEIRS, y.old_start, y.old_count, y.old_start + ours_size - base_size, y.old_count, y.new_start, y.new_count)
    return regions


def refine_conflicts(ours: Sequence[str], theirs: Sequence[str], regions: Sequence[Region], algorithm: str, rediff: bool = True) -> List[Region]:
    refined: List[Region] = []
    for region in regions:
        if region.mode != CONFLICT or region.ours_count == 0 or region.theirs_count == 0:
            refined.append(region)
            continue
        inner = diff(ours[region.ours_start:region.ours_start + region.ours_count], theirs[region.theirs_start:region.theirs_start + region.theirs_count], algorithm, rediff)
        if not inner:
            region.mode = SAME
            refined.append(region)
            continue
        for change in inner:
            refined.append(Region(CONFLICT, region.base_start, region.base_count, region.ours_start + change.old_start, change.old_count, region.theirs_start + change.new_start, change.new_count))
    return refined


def has_alnum(lines: Sequence[str]) -> bool:
    return any(ch.isascii() and ch.isalnum() for line in lines for ch in line)


def simplify_conflicts(ours: Sequence[str], regions: List[Region], when_no_alnum: bool) -> None:
    k = 0
    while k + 1 < len(regions):
        current, following = regions[k], regions[k + 1]
        begin, end = current.ours_start + current.ours_count, following.ours_start
        if current.mode != CONFLICT or following.mode != CONFLICT or (end - begin > 3 and (not when_no_alnum or has_alnum(ours[begin:end]))):
            k += 1
            continue
        current.ours_count = following.ours_start + following.ours_count - current.ours_start
        current.theirs_count = following.theirs_start + following.theirs_count - current.theirs_start
        del regions[k + 1]


def trim_conflicts(ours: Sequence[str], theirs: Sequence[str], regions: Sequence[Region]) -> None:
    for region in regions:
        if region.mode != CONFLICT:
            continue
        while region.ours_count and region.theirs_count and ours[region.ours_start] == theirs[region.theirs_start]:
            region.ours_start += 1
            region.theirs_start += 1
            region.ours_count -= 1
            region.theirs_count -= 1
        while region.ours_count and region.theirs_count and ours[region.ours_start + region.ours_count - 1] == theirs[region.theirs_start + region.theirs_count - 1]:
            region.ours_count -= 1
            region.theirs_count -= 1


def with_newline(lines: Sequence[str]) -> List[str]:
    out = list(lines)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    return out


def marker(char: str, size: int, label: Optional[str]) -> str:
    return char * size + ("" if label is None else " " + label) + "\n"


def render(base: Sequence[str], ours: Sequence[str], theirs: Sequence[str], regions: Sequence[Region], style: str, labels: Sequence[Optional[str]], favor: int, marker_size: int) -> Tuple[List[str], int]:
    out: List[str] = []
    conflicts, i = 0, 0
    for region in regions:
        if favor and region.mode == CONFLICT:
            region.mode = favor
        ours_part = ours[region.ours_start:region.ours_start + region.ours_count]
        theirs_part = theirs[region.theirs_start:region.theirs_start + region.theirs_count]
        if region.mode == CONFLICT:
            conflicts += 1
            out.extend(ours[i:region.ours_start])
            out.append(marker("<", marker_size, labels[0]))
            out.extend(with_newline(ours_part))
            if style != "merge":
                out.append(marker("|", marker_size, labels[1]))
                out.extend(with_newline(base[region.base_start:region.base_start + region.base_count]))
            out.append(marker("=", marker_size, None))
            out.extend(with_newline(theirs_part))
            out.append(marker(">", marker_size, labels[2]))
        elif region.mode in (OURS, THEIRS, UNION):
            out.extend(ours[i:region.ours_start])
            if region.mode in (OURS, UNION):
                out.extend(with_newline(ours_part) if region.mode == UNION else ours_part)
            if region.mode in (THEIRS, UNION):
                out.extend(theirs_part)
        else:
            continue
        i = region.ours_start + region.ours_count
    out.extend(ours[i:])
    return out, conflicts


def merge3(base: Sequence[str], ours: Sequence[str], theirs: Sequence[str], style: str = "merge", algorithm: str = "myers", labels: Sequence[Optional[str]] = ("ours", "base", "theirs"), favor: str = "", level: str = "zealous-alnum", marker_size: int = MARKER_SIZE, rediff: bool = True) -> MergeResult:
    if style not in STYLES:
        raise MergeError(f"unknown conflict style {style!r} (choose from {', '.join(STYLES)})")
    if level not in LEVELS or favor not in FAVORS:
        raise MergeError(f"unknown level {level!r} or favor {favor!r}")
    left, right = diff(base, ours, algorithm, rediff), diff(base, theirs, algorithm, rediff)
    if not left:
        return MergeResult(list(theirs), 0, [])
    if not right:
        return MergeResult(list(ours), 0, [])
    rank = LEVELS.index(level)
    if style != "merge":
        rank = min(rank, LEVELS.index("eager"))
    regions = pair_changes((len(base), len(ours), len(theirs)), ours, theirs, left, right, rank)
    if style == "zdiff3":
        trim_conflicts(ours, theirs, regions)
    elif rank >= LEVELS.index("zealous"):
        regions = refine_conflicts(ours, theirs, regions, algorithm, rediff)
        simplify_conflicts(ours, regions, rank > LEVELS.index("zealous"))
    lines, conflicts = render(base, ours, theirs, regions, style, labels, FAVORS[favor], marker_size)
    return MergeResult(lines, conflicts, regions)


def generations(graph: Graph) -> Dict[str, int]:
    depth: Dict[str, int] = {}
    for root in graph:
        if root in depth:
            continue
        stack = [root]
        while stack:
            commit = stack[-1]
            pending = [p for p in graph.get(commit, ()) if p not in depth]
            if pending:
                stack.extend(pending)
                continue
            stack.pop()
            depth[commit] = 1 + max((depth[p] for p in graph.get(commit, ())), default=0)
    return depth


def is_ancestor(graph: Graph, ancestor: str, descendant: str, depth: Optional[Dict[str, int]] = None) -> bool:
    depth = depth or generations(graph)
    seen: Set[str] = set()
    stack = [descendant]
    while stack:
        commit = stack.pop()
        if commit == ancestor:
            return True
        if commit in seen or depth.get(commit, 0) < depth.get(ancestor, 0):
            continue
        seen.add(commit)
        stack.extend(graph.get(commit, ()))
    return False


def independent(graph: Graph, commits: Sequence[str], depth: Dict[str, int]) -> List[str]:
    keep = [c for c in commits if not any(o != c and is_ancestor(graph, c, o, depth) for o in commits)]
    return sorted(keep, key=lambda c: (-depth.get(c, 0), c))


def merge_bases(graph: Graph, one: str, two: str) -> List[str]:
    for commit in (one, two):
        if commit not in graph:
            raise MergeError(f"{commit} is not in the commit graph")
    depth = generations(graph)
    flags: Dict[str, int] = {one: PARENT1}
    flags[two] = flags.get(two, 0) | PARENT2
    queue = [(-depth[one], 0, one), (-depth[two], 1, two)]
    heapq.heapify(queue)
    order = 2
    candidates: List[str] = []
    while any(not flags[c] & STALE for _, _, c in queue):
        _, _, commit = heapq.heappop(queue)
        paint = flags[commit] & (PARENT1 | PARENT2 | STALE)
        if paint == PARENT1 | PARENT2:
            if not flags[commit] & RESULT:
                flags[commit] |= RESULT
                candidates.append(commit)
            paint |= STALE
        for parent in graph.get(commit, ()):
            if flags.get(parent, 0) & paint == paint:
                continue
            flags[parent] = flags.get(parent, 0) | paint
            heapq.heappush(queue, (-depth.get(parent, 1), order, parent))
            order += 1
    return independent(graph, candidates, depth)


def short(commit: str) -> str:
    return commit if commit.startswith("virtual") else commit[:7]


class Store:
    def __init__(self, loader: Callable[[str], List[str]]) -> None:
        self.loader = loader
        self.cache: Dict[str, List[str]] = {}

    def __call__(self, commit: str) -> List[str]:
        if commit not in self.cache:
            self.cache[commit] = self.loader(commit)
        return self.cache[commit]


def merge_commits(graph: Graph, ours: str, theirs: str, store: Store, algorithm: str = "histogram", style: str = "merge", names: Tuple[str, str] = ("ours", "theirs"), depth: int = 0, steps: Optional[List[str]] = None, rediff: bool = True) -> Tuple[MergeResult, List[str]]:
    steps = [] if steps is None else steps
    bases = merge_bases(graph, ours, theirs)
    indent = "  " * depth
    steps.append(f"{indent}merge {short(ours)} and {short(theirs)}: {len(bases)} merge base(s) {' '.join(short(b) for b in bases) or '(none)'}")
    if not bases:
        base, base_label = [], "empty tree"
    elif len(bases) == 1:
        base, base_label = store(bases[0]), short(bases[0])
    else:
        current = bases[0]
        for other in bases[1:]:
            inner, _ = merge_commits(graph, current, other, store, algorithm, style, VIRTUAL_NAMES, depth + 1, steps, rediff)
            virtual = f"virtual-{depth + 1}-{len(store.cache)}"
            graph[virtual] = (current, other)
            store.cache[virtual] = inner.lines
            steps.append(f"{indent}  {virtual} = merge of {short(current)} and {short(other)}, {inner.conflicts} conflict(s) left in it")
            current = virtual
        base, base_label = store(current), "merged common ancestors"
    result = merge3(base, store(ours), store(theirs), style, algorithm, (names[0], base_label, names[1]), level="zealous", marker_size=MARKER_SIZE + 2 * depth, rediff=rediff)
    return result, bases


def spans(data: bytes) -> Dict[int, int]:
    text = b"\0" not in data[:8000]
    counts: Dict[int, int] = {}
    n = acc1 = acc2 = 0
    for index, c in enumerate(data):
        if text and c == 13 and index + 1 < len(data) and data[index + 1] == 10:
            continue
        old1 = acc1
        acc1 = (((acc1 << 7) ^ (acc2 >> 25)) + c) & MASK
        acc2 = ((acc2 << 7) ^ (old1 >> 25)) & MASK
        n += 1
        if n < 64 and c != 10:
            continue
        key = ((acc1 + acc2 * 0x61) & MASK) % HASHBASE
        counts[key] = counts.get(key, 0) + n
        n = acc1 = acc2 = 0
    if n:
        key = ((acc1 + acc2 * 0x61) & MASK) % HASHBASE
        counts[key] = counts.get(key, 0) + n
    return counts


def similarity(source: bytes, target: bytes, minimum: int, cache: Optional[Dict[bytes, Dict[int, int]]] = None) -> int:
    largest, smallest = max(len(source), len(target)), min(len(source), len(target))
    if not target or largest * (MAX_SCORE - minimum) < (largest - smallest) * MAX_SCORE:
        return 0
    cache = {} if cache is None else cache
    for blob in (source, target):
        if blob not in cache:
            cache[blob] = spans(blob)
    theirs = cache[target]
    copied = sum(min(count, theirs.get(key, 0)) for key, count in cache[source].items())
    return copied * MAX_SCORE // largest


def exact_renames(old: Dict[str, bytes], new: Dict[str, bytes], sources: Sequence[str], targets: Sequence[str]) -> List[Rename]:
    by_blob: Dict[bytes, List[str]] = {}
    for path in sources:
        by_blob.setdefault(old[path], []).append(path)
    found: List[Rename] = []
    used: Set[str] = set()
    for target in targets:
        same = [s for s in by_blob.get(new[target], []) if s not in used]
        if same:
            named = [s for s in same if os.path.basename(s) == os.path.basename(target)]
            source = (named or same)[0]
            used.add(source)
            found.append(Rename(source, target, MAX_SCORE))
    return found


def basename_renames(old: Dict[str, bytes], new: Dict[str, bytes], sources: Sequence[str], targets: Sequence[str], minimum: int, cache: Dict[bytes, Dict[int, int]]) -> List[Rename]:
    source_names = Counter(os.path.basename(s) for s in sources)
    target_by_name = {os.path.basename(t): t for t in targets}
    target_names = Counter(os.path.basename(t) for t in targets)
    stricter = minimum + (MAX_SCORE - minimum) // 2
    found = []
    for source in sources:
        name = os.path.basename(source)
        if source_names[name] == 1 and target_names[name] == 1:
            score = similarity(old[source], new[target_by_name[name]], minimum, cache)
            if score >= stricter:
                found.append(Rename(source, target_by_name[name], score))
    return found


def best_sources(old: Dict[str, bytes], new: Dict[str, bytes], sources: Sequence[str], target: str, minimum: int, cache: Dict[bytes, Dict[int, int]]) -> List[Tuple[int, int, str, str]]:
    slots: List[Tuple[int, int, str, str]] = []
    for source in sources:
        entry = (similarity(old[source], new[target], minimum, cache), int(os.path.basename(source) == os.path.basename(target)), source, target)
        if len(slots) < CANDIDATES_PER_TARGET:
            slots.append(entry)
            continue
        worst = min(range(len(slots)), key=lambda k: (slots[k][:2], -k))
        if slots[worst][:2] < entry[:2]:
            slots[worst] = entry
    return slots


def unclaimed(found: Sequence[Rename], sources: Sequence[str], targets: Sequence[str]) -> Tuple[List[str], List[str]]:
    used, done = {r.source for r in found}, {r.target for r in found}
    return [s for s in sources if s not in used], [t for t in targets if t not in done]


def find_renames(old: Dict[str, bytes], new: Dict[str, bytes], threshold: int = 50) -> List[Rename]:
    if not 0 <= threshold <= 100:
        raise MergeError(f"--threshold must be between 0 and 100, got {threshold}")
    minimum = threshold * MAX_SCORE // 100
    sources = sorted(p for p in old if p not in new)
    targets = sorted(p for p in new if p not in old)
    cache: Dict[bytes, Dict[int, int]] = {}
    found = exact_renames(old, new, sources, targets)
    sources, targets = unclaimed(found, sources, targets)
    found += basename_renames(old, new, sources, targets, minimum, cache)
    sources, targets = unclaimed(found, sources, targets)
    used, done = {r.source for r in found}, {r.target for r in found}
    candidates = [c for target in targets for c in best_sources(old, new, sources, target, minimum, cache)]
    for score, _, source, target in sorted(candidates, key=lambda c: (-c[0], -c[1])):
        if score < minimum:
            break
        if target not in done and source not in used:
            found.append(Rename(source, target, score))
            used.add(source)
            done.add(target)
    return sorted(found, key=lambda r: r.target)


def load_tree(root: str) -> Dict[str, bytes]:
    if not os.path.isdir(root):
        raise MergeError(f"{root} is not a directory")
    tree: Dict[str, bytes] = {}
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            path = os.path.join(folder, name)
            with open(path, "rb") as handle:
                tree[os.path.relpath(path, root).replace(os.sep, "/")] = handle.read()
    return tree


def subprocess_runner(argv: Sequence[str]) -> Tuple[int, str, str]:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", check=False)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: command not found"
    return done.returncode, done.stdout, done.stderr


def git(runner: Runner, repo: str, *args: str) -> str:
    code, out, err = runner(["git", "-C", repo, *args])
    if code != 0:
        raise MergeError(f"`git {' '.join(args[:3])}` failed (exit {code}): {(err or out).strip()}")
    return out


def resolve(runner: Runner, repo: str, name: str) -> str:
    code, out, _ = runner(["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}"])
    if code != 0 or not out.strip():
        raise MergeError(f"{name} does not name a commit in {repo}")
    return out.strip()


def load_graph(runner: Runner, repo: str, *tips: str) -> Graph:
    graph: Graph = {}
    for line in git(runner, repo, "rev-list", "--parents", "--all", *tips).splitlines():
        parts = line.split()
        if parts:
            graph[parts[0]] = tuple(parts[1:])
    return graph


def read_graph(path: str) -> Graph:
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError as error:
        raise MergeError(f"{path} is not valid JSON: line {error.lineno} column {error.colno}: {error.msg}") from None
    commits = data.get("commits", data) if isinstance(data, dict) else None
    if not isinstance(commits, dict) or not all(isinstance(p, list) for p in commits.values()):
        raise MergeError(f'{path} must be {{"commits": {{"C": ["PARENT", ...]}}}}')
    return {str(c): tuple(str(p) for p in parents) for c, parents in commits.items()}


def file_at(runner: Runner, repo: str, path: str) -> Callable[[str], List[str]]:
    def load(commit: str) -> List[str]:
        code, out, _ = runner(["git", "-C", repo, "cat-file", "-p", f"{commit}:{path}"])
        return split_lines(out) if code == 0 else []
    return load


def region_json(region: Region) -> Dict[str, object]:
    return {
        "mode": MODE_NAMES[region.mode],
        "base": [region.base_start + 1, region.base_count],
        "ours": [region.ours_start + 1, region.ours_count],
        "theirs": [region.theirs_start + 1, region.theirs_count],
    }


def diff_json(old: Sequence[str], new: Sequence[str], changes: Sequence[Change], algorithm: str) -> Dict[str, object]:
    return {
        "algorithm": algorithm,
        "changes": [{"old_start": c.old_start + 1, "old_count": c.old_count, "new_start": c.new_start + 1, "new_count": c.new_count} for c in changes],
        "removed": [old[i] for c in changes for i in range(c.old_start, c.old_start + c.old_count)],
        "added": [new[i] for c in changes for i in range(c.new_start, c.new_start + c.new_count)],
    }


def emit(text: str, output: str) -> None:
    if output:
        write_text(output, text)
    else:
        sys.stdout.write(text)


def run_diff(args: argparse.Namespace) -> int:
    old, new = split_lines(read_text(args.old)), split_lines(read_text(args.new))
    changes = diff(old, new, args.algorithm, not args.no_rediff)
    labels = (args.label + [args.old, args.new][len(args.label):])[:2]
    if args.json:
        print(json.dumps(diff_json(old, new, changes, args.algorithm), indent=2))
    else:
        sys.stdout.write(unified(old, new, changes, args.context, labels[0], labels[1]))
    return 1 if changes else 0


def run_merge_file(args: argparse.Namespace) -> int:
    if len(args.labels) > 3:
        raise MergeError("-L can be given at most three times (ours, base, theirs)")
    labels = args.labels + [args.ours, args.base, args.theirs][len(args.labels):]
    style = "zdiff3" if args.zdiff3 else "diff3" if args.diff3 else args.style
    texts = [split_lines(read_text(p)) for p in (args.base, args.ours, args.theirs)]
    result = merge3(texts[0], texts[1], texts[2], style, args.algorithm, labels, args.favor, args.level, args.marker_size, not args.no_rediff)
    text = "".join(result.lines)
    if args.json:
        print(json.dumps({"clean": result.conflicts == 0, "conflicts": result.conflicts, "style": style, "algorithm": args.algorithm, "regions": [region_json(r) for r in result.regions], "result": text}, indent=2))
    else:
        emit(text, args.output)
    if result.conflicts:
        print(f"minimerge: {result.conflicts} conflict(s)", file=sys.stderr)
    return 1 if result.conflicts else 0


def run_merge_base(args: argparse.Namespace, runner: Runner) -> int:
    if args.graph:
        graph = read_graph(args.graph)
        one, two = args.one, args.two
    else:
        one, two = resolve(runner, args.repo, args.one), resolve(runner, args.repo, args.two)
        graph = load_graph(runner, args.repo, one, two)
    bases = merge_bases(graph, one, two)
    if args.json:
        print(json.dumps({"one": one, "two": two, "bases": bases, "criss_cross": len(bases) > 1}, indent=2))
    else:
        for base in bases:
            print(base)
    if not bases:
        print(f"minimerge: {args.one} and {args.two} have no common ancestor", file=sys.stderr)
    return 0 if bases else 1


def run_merge_recursive(args: argparse.Namespace, runner: Runner) -> int:
    ours, theirs = resolve(runner, args.repo, args.ours), resolve(runner, args.repo, args.theirs)
    graph = load_graph(runner, args.repo, ours, theirs)
    steps: List[str] = []
    result, bases = merge_commits(graph, ours, theirs, Store(file_at(runner, args.repo, args.path)), args.algorithm, args.style, (args.ours, args.theirs), 0, steps, not args.no_rediff)
    text = "".join(result.lines)
    if args.json:
        print(json.dumps({"bases": bases, "virtual_base": len(bases) > 1, "steps": steps, "conflicts": result.conflicts, "result": text}, indent=2))
    else:
        emit(text, args.output)
        for step in steps:
            print(f"minimerge: {step}", file=sys.stderr)
    return 1 if result.conflicts else 0


def run_renames(args: argparse.Namespace) -> int:
    old, new = load_tree(args.old_dir), load_tree(args.new_dir)
    found = find_renames(old, new, args.threshold)
    sources, targets = {r.source for r in found}, {r.target for r in found}
    deleted = sorted(p for p in old if p not in new and p not in sources)
    added = sorted(p for p in new if p not in old and p not in targets)
    if args.json:
        print(json.dumps({"renames": [dict(asdict(r), similarity=r.similarity) for r in found], "deleted": deleted, "added": added}, indent=2))
    else:
        for rename in found:
            print(f"R{rename.similarity:03d}\t{rename.source}\t{rename.target}")
        for path in deleted:
            print(f"D\t{path}")
        for path in added:
            print(f"A\t{path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minimerge", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("diff", help="unified diff of two files; exit 1 when they differ")
    d.add_argument("old")
    d.add_argument("new")
    d.add_argument("--algorithm", "--diff-algorithm", choices=ALGORITHMS, default="myers")
    d.add_argument("-U", "--unified", dest="context", type=int, default=3, help="lines of context (default 3)")
    d.add_argument("--label", action="append", default=[], help="name for the --- line, then the +++ line")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.add_argument("--no-rediff", action="store_true", help="histogram only: skip the re-diff of shifted groups (Git before 2.54)")
    m = sub.add_parser("merge-file", help="three-way merge, arguments in git merge-file order: OURS BASE THEIRS")
    m.add_argument("ours")
    m.add_argument("base")
    m.add_argument("theirs")
    m.add_argument("--style", choices=STYLES, default="merge")
    m.add_argument("--diff3", action="store_true", help="same as --style diff3")
    m.add_argument("--zdiff3", action="store_true", help="same as --style zdiff3")
    m.add_argument("--algorithm", "--diff-algorithm", choices=ALGORITHMS, default="myers")
    m.add_argument("--level", choices=LEVELS, default="zealous-alnum", help="how hard to shrink conflicts (git merge-file: zealous-alnum, git merge: zealous)")
    m.add_argument("--favor", choices=("ours", "theirs", "union"), default="", help="resolve every conflict this way")
    m.add_argument("--marker-size", type=int, default=MARKER_SIZE)
    m.add_argument("-L", dest="labels", action="append", default=[], help="labels for ours, base, theirs")
    m.add_argument("-o", "--output", default="", help="write the result here instead of stdout")
    m.add_argument("--json", action="store_true", help="machine-readable output")
    m.add_argument("--no-rediff", action="store_true", help="histogram only: skip the re-diff of shifted groups (Git before 2.54)")
    b = sub.add_parser("merge-base", help="all best common ancestors of two commits; exit 1 if there is none")
    b.add_argument("one")
    b.add_argument("two")
    source = b.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", help="a Git repository")
    source.add_argument("--graph", help='a JSON file {"commits": {"C": ["PARENT", ...]}}')
    b.add_argument("--json", action="store_true", help="machine-readable output")
    r = sub.add_parser("merge-recursive", help="merge one file between two commits, building a virtual base for criss-cross histories")
    r.add_argument("ours")
    r.add_argument("theirs")
    r.add_argument("path")
    r.add_argument("--repo", default=".")
    r.add_argument("--algorithm", "--diff-algorithm", choices=ALGORITHMS, default="histogram")
    r.add_argument("--style", choices=STYLES, default="merge")
    r.add_argument("-o", "--output", default="")
    r.add_argument("--json", action="store_true", help="machine-readable output")
    r.add_argument("--no-rediff", action="store_true", help="histogram only: skip the re-diff of shifted groups (Git before 2.54)")
    n = sub.add_parser("renames", help="deleted and added files paired by content similarity, like git diff -M")
    n.add_argument("old_dir")
    n.add_argument("new_dir")
    n.add_argument("--threshold", type=int, default=50, help="minimum similarity in percent (default 50, as git -M)")
    n.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv: Optional[Sequence[str]] = None, runner: Runner = subprocess_runner) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "diff":
            if args.context < 0:
                raise MergeError("-U must be zero or more")
            return run_diff(args)
        if args.command == "merge-file":
            return run_merge_file(args)
        if args.command == "merge-base":
            return run_merge_base(args, runner)
        if args.command == "merge-recursive":
            return run_merge_recursive(args, runner)
        return run_renames(args)
    except MergeError as error:
        print(f"minimerge: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
