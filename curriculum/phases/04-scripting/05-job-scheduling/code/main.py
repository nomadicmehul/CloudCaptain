"""minicron: parse cron expressions and systemd OnCalendar specs, compute next runs in a time zone with daylight-saving gaps and folds resolved by an explicit policy, simulate a scheduler (overlap, missed runs, jitter, catch-up) on a virtual clock with a run journal, and run one job under a lock with a timeout."""
from __future__ import annotations

import argparse
import calendar
import errno
import hashlib
import heapq
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from itertools import islice
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Optional, Sequence, Tuple

try:
    import fcntl
except ImportError:
    fcntl = None

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
MAX_YEARS = 10
MAX_GAP = timedelta(days=366 * MAX_YEARS)
MAX_SIM_RUNS = 100000
SAFE_MARGIN = timedelta(hours=26)
EXIT_LOCKED = 75
EXIT_TIMEOUT = 124
DIALECTS = ("vixie", "robfig")
GAP_POLICIES = ("auto", "shift", "skip")
FOLD_POLICIES = ("auto", "both", "first")
CONCURRENCY = ("Allow", "Forbid", "Replace")
MISSED = ("skip", "once", "all")
MONTH_NAMES = {name: i + 1 for i, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
CRON_DAY_NAMES = {name: i for i, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
CRON_FIELDS = (
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day-of-month", 1, 31, {}),
    ("month", 1, 12, MONTH_NAMES),
    ("day-of-week", 0, 7, CRON_DAY_NAMES),
)
MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
CAL_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
CAL_DAY_LONG = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
CAL_DAY_NAMES = {**{d.lower(): i for i, d in enumerate(CAL_DAYS)}, **{d: i for i, d in enumerate(CAL_DAY_LONG)}}
SHORTHANDS = {
    "minutely": "*-*-* *:*:00",
    "hourly": "*-*-* *:00:00",
    "daily": "*-*-* 00:00:00",
    "weekly": "Mon *-*-* 00:00:00",
    "monthly": "*-*-01 00:00:00",
    "yearly": "*-01-01 00:00:00",
    "annually": "*-01-01 00:00:00",
    "quarterly": "*-01,04,07,10-01 00:00:00",
    "semiannually": "*-01,07-01 00:00:00",
}
CAL_COMPONENT = re.compile(r"^(\d+)(?:\.\.(\d+))?(?:/(\d+))?$")
ZONE_TOKEN = re.compile(r"^(UTC|[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+)+)$")


class ScheduleError(ValueError):
    pass


def load_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"unknown time zone {name!r} (expected an IANA name such as Europe/Berlin)")


def format_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_instant(text: str, zone: ZoneInfo) -> datetime:
    value = text.strip()
    if value == "now":
        return datetime.now(UTC).replace(microsecond=0)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ScheduleError(f"{text!r} is not an ISO 8601 time such as 2026-03-28T12:00:00Z")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment.astimezone(UTC)


def cron_weekday(day: date) -> int:
    return day.isoweekday() % 7


def cron_value(token: str, name: str, lo: int, hi: int, names: Dict[str, int]) -> int:
    lowered = token.lower()
    if names and lowered in names:
        return names[lowered]
    if not token.isdigit():
        kind = "a number or a name" if names else "a number"
        raise ScheduleError(f"{name}: {token!r} is not {kind}")
    value = int(token)
    if not lo <= value <= hi:
        raise ScheduleError(f"{name}: {value} is outside {lo}-{hi}")
    return value


def parse_cron_field(text: str, name: str, lo: int, hi: int, names: Dict[str, int]) -> Tuple[FrozenSet[int], bool, bool]:
    open_end = 6 if name == "day-of-week" else hi
    values = set()
    robfig_star = False
    for item in text.split(","):
        if not item:
            raise ScheduleError(f"{name}: empty item in {text!r}")
        base, slash, step_text = item.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) == 0:
                raise ScheduleError(f"{name}: step {step_text!r} must be a positive number")
            step = int(step_text)
        if base == "*":
            start, end = lo, open_end
            robfig_star = robfig_star or step == 1
        elif "-" in base:
            first, _, last = base.partition("-")
            start, end = cron_value(first, name, lo, hi, names), cron_value(last, name, lo, hi, names)
            if start > end:
                raise ScheduleError(f"{name}: range {base} runs backwards")
        else:
            start = cron_value(base, name, lo, hi, names)
            end = open_end if slash else start
        values.update(range(start, end + 1, step))
    if name == "day-of-week" and 7 in values:
        values.discard(7)
        values.add(0)
    return frozenset(values), text.startswith("*"), robfig_star


@dataclass(frozen=True)
class CronExpr:
    text: str
    fields: Tuple[str, ...]
    minutes: Tuple[int, ...]
    hours: Tuple[int, ...]
    days: FrozenSet[int]
    months: FrozenSet[int]
    weekdays: FrozenSet[int]
    dom_star: bool
    dow_star: bool
    dialect: str

    @property
    def fixed_time(self) -> bool:
        return not (self.fields[0].startswith("*") or self.fields[1].startswith("*"))

    @property
    def day_rule(self) -> str:
        return "and" if self.dom_star or self.dow_star else "or"

    def default_dst(self) -> Tuple[str, str]:
        return ("shift", "first") if self.fixed_time else ("skip", "both")

    def matches_date(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        in_month = day.day in self.days
        in_week = cron_weekday(day) in self.weekdays
        if self.day_rule == "and":
            return in_month and in_week
        return in_month or in_week

    def wall_times(self, start: date) -> Iterator[datetime]:
        day = last_match = start
        while day - last_match <= MAX_GAP:
            if self.matches_date(day):
                last_match = day
                for hour in self.hours:
                    for minute in self.minutes:
                        yield datetime(day.year, day.month, day.day, hour, minute)
            day += timedelta(days=1)


def parse_cron(text: str, dialect: str = "vixie") -> CronExpr:
    if dialect not in DIALECTS:
        raise ScheduleError(f"unknown dialect {dialect!r}; use one of {', '.join(DIALECTS)}")
    stripped = text.strip()
    body = stripped
    if stripped.startswith("@"):
        key = stripped.lower()
        if key == "@reboot":
            raise ScheduleError("@reboot runs once when the cron daemon starts; it has no next run time")
        if key not in MACROS:
            raise ScheduleError(f"unknown macro {stripped!r}; known: {', '.join(sorted(MACROS))}")
        body = MACROS[key]
    if stripped.upper().startswith(("CRON_TZ=", "TZ=")):
        raise ScheduleError("put the time zone in --tz (or a CronJob's timeZone field), not in the expression")
    parts = body.split()
    if len(parts) != 5:
        hint = ""
        if len(parts) == 6:
            hint = "; six fields is either a Quartz-style seconds field or an /etc/cron.d line with its user field"
        raise ScheduleError(f"expected 5 fields (minute hour day-of-month month day-of-week), got {len(parts)}{hint}")
    parsed = [parse_cron_field(part, name, lo, hi, names) for part, (name, lo, hi, names) in zip(parts, CRON_FIELDS)]
    star_index = 1 if dialect == "vixie" else 2
    return CronExpr(
        text=stripped,
        fields=tuple(parts),
        minutes=tuple(sorted(parsed[0][0])),
        hours=tuple(sorted(parsed[1][0])),
        days=parsed[2][0],
        months=parsed[3][0],
        weekdays=parsed[4][0],
        dom_star=parsed[2][star_index],
        dow_star=parsed[4][star_index],
        dialect=dialect,
    )


@dataclass(frozen=True)
class Component:
    start: int
    stop: Optional[int] = None
    repeat: Optional[int] = None

    def values(self, hi: int) -> range:
        if self.stop is None and self.repeat is None:
            return range(self.start, self.start + 1)
        stop = hi if self.stop is None else self.stop
        return range(self.start, stop + 1, self.repeat or 1)

    def format(self, width: int) -> str:
        text = f"{self.start:0{width}d}"
        if self.stop is not None:
            text += f"..{self.stop:0{width}d}"
        if self.repeat is not None:
            text += f"/{self.repeat}"
        return text


CalField = Optional[Tuple[Component, ...]]


def parse_cal_field(text: str, name: str, lo: int, hi: int) -> CalField:
    if text == "*":
        return None
    components = set()
    for item in text.split(","):
        match = CAL_COMPONENT.match(item)
        if not match:
            raise ScheduleError(f"{name}: {item!r} is not N, N..M, N/R or N..M/R")
        start = int(match.group(1))
        stop = None if match.group(2) is None else int(match.group(2))
        repeat = None if match.group(3) is None else int(match.group(3))
        for value in (start, stop):
            if value is not None and not lo <= value <= hi:
                raise ScheduleError(f"{name}: {value} is outside {lo}..{hi}")
        if stop is not None and stop < start:
            raise ScheduleError(f"{name}: range {item} runs backwards")
        if repeat == 0:
            raise ScheduleError(f"{name}: repetition must be positive")
        if repeat == 1 and stop is None:
            stop = hi
        if repeat == 1:
            repeat = None
        if stop is not None and repeat is not None:
            stop = start + ((stop - start) // repeat) * repeat
            if stop == start:
                stop = repeat = None
        components.add(Component(start, stop, repeat))
    return tuple(sorted(components, key=lambda c: (c.start, -1 if c.stop is None else c.stop, c.repeat or 0)))


def cal_values(spec: CalField, lo: int, hi: int) -> List[int]:
    if spec is None:
        return list(range(lo, hi + 1))
    return sorted({v for c in spec for v in c.values(hi) if lo <= v <= hi})


def format_cal_field(spec: CalField, width: int) -> str:
    if spec is None:
        return "*"
    return ",".join(c.format(width) for c in spec)


def parse_weekdays(text: str) -> FrozenSet[int]:
    days = set()
    for item in text.split(","):
        first, dots, last = item.partition("..")
        try:
            start = CAL_DAY_NAMES[first.lower()]
            end = CAL_DAY_NAMES[last.lower()] if dots else start
        except KeyError:
            raise ScheduleError(f"weekday: {item!r} is not a day name or a range such as Mon..Fri")
        if end < start:
            raise ScheduleError(f"weekday: {item} wraps around the week; write it as two ranges")
        days.update(range(start, end + 1))
    return frozenset(days)


def format_weekdays(days: FrozenSet[int]) -> str:
    ordered = sorted(days)
    runs: List[List[int]] = []
    for day in ordered:
        if runs and runs[-1][-1] == day - 1:
            runs[-1].append(day)
        else:
            runs.append([day])
    parts = []
    for run in runs:
        if len(run) >= 3:
            parts.append(f"{CAL_DAYS[run[0]]}..{CAL_DAYS[run[-1]]}")
        else:
            parts.extend(CAL_DAYS[d] for d in run)
    return ",".join(parts)


@dataclass(frozen=True)
class CalendarSpec:
    text: str
    weekdays: Optional[FrozenSet[int]]
    years: CalField
    months: CalField
    days: CalField
    from_end: bool
    hours: CalField
    minutes: CalField
    seconds: CalField
    zone: Optional[str]

    @property
    def normalized(self) -> str:
        prefix = format_weekdays(self.weekdays) + " " if self.weekdays else ""
        separator = "~" if self.from_end else "-"
        text = (
            f"{prefix}{format_cal_field(self.years, 4)}-{format_cal_field(self.months, 2)}{separator}{format_cal_field(self.days, 2)} "
            f"{format_cal_field(self.hours, 2)}:{format_cal_field(self.minutes, 2)}:{format_cal_field(self.seconds, 2)}"
        )
        return text + (f" {self.zone}" if self.zone else "")

    def default_dst(self) -> Tuple[str, str]:
        return ("skip", "first")

    def matches_date(self, day: date) -> bool:
        if self.weekdays is not None and day.weekday() not in self.weekdays:
            return False
        if self.years is not None and day.year not in cal_values(self.years, 1970, 2199):
            return False
        if self.months is not None and day.month not in cal_values(self.months, 1, 12):
            return False
        if self.days is None:
            return True
        number = calendar.monthrange(day.year, day.month)[1] - day.day + 1 if self.from_end else day.day
        return number in cal_values(self.days, 1, 31)

    def wall_times(self, start: date) -> Iterator[datetime]:
        hours = cal_values(self.hours, 0, 23)
        minutes = cal_values(self.minutes, 0, 59)
        seconds = cal_values(self.seconds, 0, 59)
        day = last_match = start
        while day - last_match <= MAX_GAP:
            if self.matches_date(day):
                last_match = day
                for hour in hours:
                    for minute in minutes:
                        for second in seconds:
                            yield datetime(day.year, day.month, day.day, hour, minute, second)
            day += timedelta(days=1)


def parse_calendar(text: str) -> CalendarSpec:
    tokens = text.split()
    if not tokens:
        raise ScheduleError("empty calendar specification")
    zone = None
    if len(tokens) > 1 and ZONE_TOKEN.match(tokens[-1]):
        zone = tokens.pop()
        load_zone(zone)
    if len(tokens) == 1 and tokens[0].lower() in SHORTHANDS:
        tokens = SHORTHANDS[tokens[0].lower()].split()
    weekday_text = date_text = time_text = None
    for position, token in enumerate(tokens):
        if ":" in token:
            if time_text is not None:
                raise ScheduleError(f"two time parts: {time_text!r} and {token!r}")
            time_text = token
        elif token[0].isalpha():
            if position != 0:
                raise ScheduleError(f"weekdays must come first, found {token!r}")
            weekday_text = token
        else:
            if date_text is not None or time_text is not None:
                raise ScheduleError(f"unexpected {token!r}; the order is [weekdays] [date] [time] [zone]")
            date_text = token
    weekdays = parse_weekdays(weekday_text) if weekday_text else None
    from_end = False
    years = months = days = None
    if date_text is not None:
        if "~" in date_text:
            from_end = True
            head, day_text = date_text.rsplit("~", 1)
        elif "-" in date_text:
            head, day_text = date_text.rsplit("-", 1)
        else:
            raise ScheduleError(f"date {date_text!r} is not YYYY-MM-DD, MM-DD or MM~DD")
        year_text, dash, month_text = head.rpartition("-")
        if not dash:
            year_text = "*"
        years = parse_cal_field(year_text, "year", 1970, 2199)
        months = parse_cal_field(month_text, "month", 1, 12)
        days = parse_cal_field(day_text, "day", 1, 31)
    hours = minutes = None
    seconds: CalField = (Component(0),)
    if time_text is None:
        hours, minutes = (Component(0),), (Component(0),)
    else:
        parts = time_text.split(":")
        if len(parts) not in (2, 3):
            raise ScheduleError(f"time {time_text!r} is not HH:MM or HH:MM:SS")
        hours = parse_cal_field(parts[0], "hour", 0, 23)
        minutes = parse_cal_field(parts[1], "minute", 0, 59)
        if len(parts) == 3:
            seconds = parse_cal_field(parts[2], "second", 0, 59)
    return CalendarSpec(text.strip(), weekdays, years, months, days, from_end, hours, minutes, seconds, zone)


Schedule = Any


@dataclass(frozen=True)
class Run:
    instant: datetime
    local: datetime
    wall: datetime
    note: str = ""

    def to_json(self) -> Dict[str, str]:
        return {"utc": format_utc(self.instant), "local": self.local.isoformat(), "weekday": self.local.strftime("%a"), "note": self.note}


def resolve_policy(schedule: Schedule, gap: str = "auto", fold: str = "auto") -> Tuple[str, str]:
    if gap not in GAP_POLICIES or fold not in FOLD_POLICIES:
        raise ScheduleError(f"gap must be one of {GAP_POLICIES}, fold one of {FOLD_POLICIES}")
    default_gap, default_fold = schedule.default_dst()
    return (default_gap if gap == "auto" else gap, default_fold if fold == "auto" else fold)


def transition_instant(before: datetime, after: datetime, zone: ZoneInfo) -> datetime:
    target = after.astimezone(zone).utcoffset()
    low, high = int(before.timestamp()), int(after.timestamp())
    while high - low > 1:
        middle = (low + high) // 2
        if datetime.fromtimestamp(middle, UTC).astimezone(zone).utcoffset() == target:
            high = middle
        else:
            low = middle
    return datetime.fromtimestamp(high, UTC)


def resolve_wall(wall: datetime, zone: ZoneInfo, gap: str, fold: str) -> List[Tuple[datetime, str]]:
    first = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
    second = wall.replace(tzinfo=zone, fold=1).astimezone(UTC)
    if first == second:
        return [(first, "")]
    first_real = first.astimezone(zone).replace(tzinfo=None) == wall
    second_real = second.astimezone(zone).replace(tzinfo=None) == wall
    if first_real and second_real:
        clock = wall.strftime("%H:%M")
        if fold == "first":
            return [(first, f"fold: {clock} happens twice, first one only")]
        return [(first, f"fold: {clock} happens twice, first"), (second, f"fold: {clock} happens twice, second")]
    if gap == "skip":
        return []
    instant = transition_instant(min(first, second), max(first, second), zone)
    return [(instant, f"gap: {wall:%H:%M} does not exist, run at the end of the gap")]


def iter_runs(schedule: Schedule, zone: ZoneInfo, after: datetime, gap: str = "auto", fold: str = "auto") -> Iterator[Run]:
    gap, fold = resolve_policy(schedule, gap, fold)
    start_wall = after.astimezone(zone).replace(tzinfo=None)
    pending: Dict[datetime, Tuple[datetime, str]] = {}
    heap: List[datetime] = []

    def release(instant: datetime) -> Run:
        wall, note = pending.pop(instant)
        return Run(instant, instant.astimezone(zone), wall, note)

    for wall in schedule.wall_times((start_wall - timedelta(days=1)).date()):
        for instant, note in resolve_wall(wall, zone, gap, fold):
            if instant <= after:
                continue
            if instant in pending:
                if not note:
                    pending[instant] = (wall, note)
                continue
            pending[instant] = (wall, note)
            heapq.heappush(heap, instant)
        horizon = wall.replace(tzinfo=UTC) - SAFE_MARGIN
        while heap and heap[0] < horizon:
            yield release(heapq.heappop(heap))
    while heap:
        yield release(heapq.heappop(heap))


def next_runs(schedule: Schedule, zone: ZoneInfo, after: datetime, count: int, gap: str = "auto", fold: str = "auto") -> List[Run]:
    runs = list(islice(iter_runs(schedule, zone, after, gap, fold), count))
    if not runs:
        raise ScheduleError(f"{schedule.text!r} never runs in the next {MAX_YEARS} years")
    return runs


def runs_between(schedule: Schedule, zone: ZoneInfo, after: datetime, until: datetime, gap: str = "auto", fold: str = "auto", limit: int = MAX_SIM_RUNS) -> List[Run]:
    runs = []
    for run in iter_runs(schedule, zone, after, gap, fold):
        if run.instant > until:
            break
        runs.append(run)
        if len(runs) > limit:
            raise ScheduleError(f"{schedule.text!r} fires more than {limit} times in the window")
    return runs


def timer_activation(spec: Schedule, zone: ZoneInfo, activated: datetime, last_trigger: Optional[datetime], persistent: bool) -> Dict[str, Any]:
    missed = runs_between(spec, zone, last_trigger, activated) if last_trigger is not None else []
    upcoming = next_runs(spec, zone, activated, 1)[0]
    return {
        "missed": len(missed),
        "last_missed": format_utc(missed[-1].instant) if missed else None,
        "fires_on_activation": bool(persistent and missed),
        "next": format_utc(upcoming.instant),
    }


def jitter_seconds(key: str, max_seconds: int) -> int:
    if max_seconds <= 0:
        return 0
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % max_seconds


def compact(values: Sequence[int], names: Optional[Sequence[str]] = None) -> str:
    label = (lambda v: names[v]) if names else str
    ordered = sorted(values)
    parts = []
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1] == ordered[end] + 1:
            end += 1
        if end - index >= 2:
            parts.append(f"{label(ordered[index])}-{label(ordered[end])}")
        else:
            parts.extend(label(v) for v in ordered[index:end + 1])
        index = end + 1
    return ",".join(parts)


def explain_cron(expr: CronExpr) -> Dict[str, Any]:
    other = parse_cron(expr.text, "robfig" if expr.dialect == "vixie" else "vixie")
    months = [""] + [m.capitalize() for m in MONTH_NAMES]
    days = [d.capitalize() for d in CRON_DAY_NAMES]
    if expr.day_rule == "or":
        rule = "OR: both day fields are restricted, so a day matches if either matches"
    else:
        starred = "day-of-month" if expr.dom_star else "day-of-week"
        rule = f"AND: {starred} counts as '*' in the {expr.dialect} dialect, so both fields must match"
    gap, fold = expr.default_dst()
    if expr.fixed_time:
        dst = "fixed-time job (cronie rule): a skipped time runs once at the end of the gap, a repeated time runs once"
    else:
        dst = "wildcard job (cronie rule): skipped times do not run, repeated times run again"
    return {
        "expression": expr.text,
        "dialect": expr.dialect,
        "minute": compact(expr.minutes),
        "hour": compact(expr.hours),
        "day_of_month": compact(sorted(expr.days)),
        "month": compact(sorted(expr.months), months),
        "day_of_week": compact(sorted(expr.weekdays), days),
        "day_rule": expr.day_rule,
        "day_rule_text": rule,
        "dialects_disagree": other.day_rule != expr.day_rule,
        "dst": {"gap": gap, "fold": fold, "text": dst},
    }


@dataclass
class Job:
    name: str
    schedule: Schedule
    zone: ZoneInfo
    duration: int
    concurrency: str = "Allow"
    missed: str = "skip"
    jitter: int = 0
    deadline: Optional[int] = None
    gap: str = "auto"
    fold: str = "auto"


def load_jobs(items: Any) -> List[Job]:
    if not isinstance(items, list) or not items:
        raise ScheduleError("the jobs file must hold a non-empty JSON list")
    jobs = []
    names = set()
    for index, item in enumerate(items):
        where = f"job {index}"
        if not isinstance(item, dict) or not item.get("name"):
            raise ScheduleError(f"{where}: needs a name")
        name = str(item["name"])
        where = f"job {name!r}"
        if name in names:
            raise ScheduleError(f"{where}: duplicate name")
        names.add(name)
        if ("cron" in item) == ("calendar" in item):
            raise ScheduleError(f"{where}: give exactly one of cron or calendar")
        try:
            schedule = parse_cron(item["cron"], item.get("dialect", "vixie")) if "cron" in item else parse_calendar(item["calendar"])
        except ScheduleError as exc:
            raise ScheduleError(f"{where}: {exc}")
        zone_name = item.get("tz") or getattr(schedule, "zone", None) or "UTC"
        job = Job(
            name=name,
            schedule=schedule,
            zone=load_zone(zone_name),
            duration=int(item.get("duration", 60)),
            concurrency=item.get("concurrency", "Allow"),
            missed=item.get("missed", "skip"),
            jitter=int(item.get("jitter", 0)),
            deadline=None if item.get("deadline") is None else int(item["deadline"]),
            gap=item.get("gap", "auto"),
            fold=item.get("fold", "auto"),
        )
        if job.concurrency not in CONCURRENCY:
            raise ScheduleError(f"{where}: concurrency must be one of {', '.join(CONCURRENCY)}")
        if job.missed not in MISSED:
            raise ScheduleError(f"{where}: missed must be one of {', '.join(MISSED)}")
        if job.duration < 0 or job.jitter < 0:
            raise ScheduleError(f"{where}: duration and jitter must not be negative")
        jobs.append(job)
    return jobs


class VirtualClock:
    def __init__(self, now: datetime):
        self.now = now

    def advance_to(self, moment: datetime) -> None:
        if moment > self.now:
            self.now = moment


@dataclass
class ActiveRun:
    run_id: int
    scheduled: datetime
    finishes: datetime


class Scheduler:
    def __init__(self, jobs: List[Job], clock: VirtualClock, downtime: Sequence[Tuple[datetime, datetime]] = (), seed: str = ""):
        self.jobs = {job.name: job for job in jobs}
        self.clock = clock
        self.downtime = sorted(downtime)
        self.seed = seed
        self.journal: List[Dict[str, Any]] = []
        self.running: Dict[str, List[ActiveRun]] = {job.name: [] for job in jobs}
        self.missed: Dict[str, List[datetime]] = {job.name: [] for job in jobs}
        self.events: List[Tuple[datetime, int, int, str, Any]] = []
        self.sequence = 0

    def _push(self, moment: datetime, order: int, kind: str, data: Any) -> None:
        self.sequence += 1
        heapq.heappush(self.events, (moment, order, self.sequence, kind, data))

    def _log(self, event: str, job: str, scheduled: datetime, reason: str = "") -> None:
        self.journal.append({"time": format_utc(self.clock.now), "job": job, "event": event, "scheduled": format_utc(scheduled), "reason": reason})

    def _is_down(self, moment: datetime) -> bool:
        return any(start <= moment < end for start, end in self.downtime)

    def run_until(self, end: datetime) -> List[Dict[str, Any]]:
        start = self.clock.now
        for job in self.jobs.values():
            for run in runs_between(job.schedule, job.zone, start, end, job.gap, job.fold):
                delay = jitter_seconds(f"{self.seed}{job.name}@{format_utc(run.instant)}", job.jitter)
                self._push(run.instant + timedelta(seconds=delay), 2, "fire", (job.name, run.instant))
        for down_start, down_end in self.downtime:
            if start <= down_end <= end:
                self._push(down_end, 1, "restart", None)
        while self.events and self.events[0][0] <= end:
            moment, _, _, kind, data = heapq.heappop(self.events)
            self.clock.advance_to(moment)
            if kind == "finish":
                self._finish(*data)
            elif kind == "restart":
                self._catch_up()
            elif self._is_down(moment):
                self.missed[data[0]].append(data[1])
            else:
                self._attempt(self.jobs[data[0]], data[1], "schedule")
        self.clock.advance_to(end)
        return self.journal

    def _attempt(self, job: Job, scheduled: datetime, reason: str) -> None:
        active = self.running[job.name]
        if active and job.concurrency == "Forbid":
            self._log("skip", job.name, scheduled, "previous run still active (Forbid)")
            return
        if active and job.concurrency == "Replace":
            for run in active:
                self._log("kill", job.name, run.scheduled, "replaced by a newer run (Replace)")
            active.clear()
        self.sequence += 1
        run = ActiveRun(self.sequence, scheduled, self.clock.now + timedelta(seconds=job.duration))
        active.append(run)
        self._log("start", job.name, scheduled, reason)
        self._push(run.finishes, 0, "finish", (job.name, run.run_id))

    def _finish(self, name: str, run_id: int) -> None:
        for run in self.running[name]:
            if run.run_id == run_id:
                self.running[name].remove(run)
                self._log("finish", name, run.scheduled, "exit 0")
                return

    def _catch_up(self) -> None:
        now = self.clock.now
        for name in sorted(self.missed):
            missed, job = self.missed[name], self.jobs[name]
            self.missed[name] = []
            if not missed:
                continue
            if job.missed == "skip":
                chosen: List[datetime] = []
            elif job.missed == "once":
                chosen = missed[-1:]
            else:
                chosen = list(missed)
            for scheduled in missed:
                if scheduled not in chosen:
                    self._log("miss", name, scheduled, f"scheduler was down; missed-run policy {job.missed}")
                elif job.deadline is not None and (now - scheduled).total_seconds() > job.deadline:
                    self._log("miss", name, scheduled, f"older than the starting deadline of {job.deadline}s")
                else:
                    self._attempt(job, scheduled, "catch-up")


def simulate(jobs: List[Job], start: datetime, end: datetime, downtime: Sequence[Tuple[datetime, datetime]] = (), seed: str = "") -> List[Dict[str, Any]]:
    return Scheduler(jobs, VirtualClock(start), downtime, seed).run_until(end)


def summarize(journal: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    summary: Dict[str, Dict[str, int]] = {}
    for entry in journal:
        counts = summary.setdefault(entry["job"], {"start": 0, "finish": 0, "skip": 0, "kill": 0, "miss": 0})
        counts[entry["event"]] += 1
    return summary


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_flock(path: str) -> Optional[int]:
    if fcntl is None:
        raise OSError(errno.ENOSYS, "flock is not available on this platform")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        raise
    os.ftruncate(descriptor, 0)
    os.write(descriptor, f"{os.getpid()} {int(time.time())}\n".encode())
    return descriptor


def read_lock_owner(path: str) -> Tuple[Optional[int], Optional[int]]:
    try:
        with open(path, encoding="utf-8") as handle:
            fields = handle.read().split()
    except OSError:
        return None, None
    numbers = [int(f) for f in fields[:2] if f.isdigit()]
    return (numbers + [None, None])[0], (numbers + [None, None])[1]


def pidfile_state(owner: Optional[int], started: Optional[int], now: float, stale_after: Optional[int], alive: Callable[[int], bool] = pid_alive) -> str:
    if owner is None:
        return "stale"
    if not alive(owner):
        return "stale"
    if stale_after is not None and started is not None and now - started > stale_after:
        return "stale"
    return "held"


def acquire_pidfile(path: str, stale_after: Optional[int], alive: Callable[[int], bool] = pid_alive, now: Callable[[], float] = time.time) -> Tuple[bool, str]:
    note = ""
    for _ in range(2):
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            owner, started = read_lock_owner(path)
            if pidfile_state(owner, started, now(), stale_after, alive) == "held":
                return False, f"held by pid {owner}"
            note = f"removed a stale lock left by pid {owner}"
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(descriptor, "w") as handle:
            handle.write(f"{os.getpid()} {int(now())}\n")
        return True, note
    return False, "lost the race for a stale lock"


def exit_status(code: int) -> int:
    return 128 - code if code < 0 else code


def run_job(command: List[str], name: str, lock: Optional[str] = None, pidfile: Optional[str] = None, stale_after: Optional[int] = None, timeout: Optional[float] = None, jitter: int = 0, journal: Optional[str] = None, dry_run: bool = False, as_json: bool = False, sleep: Callable[[float], None] = time.sleep, err=None, out=None) -> int:
    err = err or sys.stderr
    out = out or sys.stdout
    delay = jitter_seconds(f"{socket.gethostname()} {name}", jitter)
    plan = {"name": name, "command": command, "delay": delay, "lock": lock, "pidfile": pidfile, "timeout": timeout, "journal": journal}
    if dry_run:
        if as_json:
            out.write(json.dumps(plan, indent=2) + "\n")
        else:
            out.write(f"would wait {delay}s, lock {lock or pidfile or 'none'}, run {' '.join(command)} with timeout {timeout or 'none'}\n")
        return 0
    if delay:
        print(f"minicron: {name}: jitter, waiting {delay}s", file=err)
        sleep(delay)
    descriptor = None
    if lock:
        descriptor = acquire_flock(lock)
        if descriptor is None:
            owner, _ = read_lock_owner(lock)
            print(f"minicron: {name}: lock {lock} is busy (pid {owner}); not starting", file=err)
            return record(journal, name, EXIT_LOCKED, "locked", 0.0)
    if pidfile:
        acquired, note = acquire_pidfile(pidfile, stale_after)
        if note:
            print(f"minicron: {name}: {note}", file=err)
        if not acquired:
            print(f"minicron: {name}: pidfile {pidfile} {note}; not starting", file=err)
            return record(journal, name, EXIT_LOCKED, "locked", 0.0)
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, start_new_session=True, pass_fds=(descriptor,) if descriptor is not None else ())
        try:
            code = exit_status(process.wait(timeout=timeout))
            outcome = "succeeded" if code == 0 else "failed"
        except subprocess.TimeoutExpired:
            print(f"minicron: {name}: timed out after {timeout}s, sending SIGTERM to the process group", file=err)
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            code, outcome = EXIT_TIMEOUT, "timeout"
    except FileNotFoundError:
        print(f"minicron: {name}: command not found: {command[0]}", file=err)
        code, outcome = 127, "failed"
    finally:
        if pidfile:
            try:
                os.unlink(pidfile)
            except FileNotFoundError:
                pass
        if descriptor is not None:
            os.close(descriptor)
    return record(journal, name, code, outcome, time.monotonic() - started)


def record(journal: Optional[str], name: str, code: int, outcome: str, seconds: float) -> int:
    if journal:
        entry = {"time": format_utc(datetime.now(UTC)), "job": name, "outcome": outcome, "exit_code": code, "seconds": round(seconds, 3)}
        with open(journal, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    return code


def print_calendar(spec: CalendarSpec, runs: List[Run], zone_name: str, out) -> None:
    out.write(f"  Original form: {spec.text}\n")
    out.write(f"Normalized form: {spec.normalized}\n")
    for index, run in enumerate(runs):
        label = "    Next elapse" if index == 0 else f"   Iteration #{index + 1}"
        out.write(f"{label}: {run.local.strftime('%a %Y-%m-%d %H:%M:%S %Z')}\n")
        if zone_name != "UTC":
            out.write(f"       (in UTC): {run.instant.strftime('%a %Y-%m-%d %H:%M:%S UTC')}\n")
        if run.note:
            out.write(f"           note: {run.note}\n")


def parse_downtime(text: str, zone: ZoneInfo) -> Tuple[datetime, datetime]:
    start, slash, end = text.partition("/")
    if not slash:
        raise ScheduleError(f"--down {text!r} must be FROM/TO")
    window = (parse_instant(start, zone), parse_instant(end, zone))
    if window[0] >= window[1]:
        raise ScheduleError(f"--down {text!r} ends before it starts")
    return window


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    policy = argparse.ArgumentParser(add_help=False)
    policy.add_argument("--tz", default="UTC", help="IANA time zone the schedule is written in (default UTC)")
    policy.add_argument("--gap", choices=GAP_POLICIES, default="auto", help="a wall time skipped by DST: shift to the end of the gap or skip (auto: cronie rule for cron, skip for OnCalendar)")
    policy.add_argument("--fold", choices=FOLD_POLICIES, default="auto", help="a wall time repeated by DST: run both or the first only")
    parser = argparse.ArgumentParser(prog="minicron", description="cron and OnCalendar schedules, DST-correct next runs, a scheduler simulation and a locked job runner")
    sub = parser.add_subparsers(dest="command", required=True)
    nxt = sub.add_parser("next", parents=[common, policy], help="next run times of a cron expression")
    nxt.add_argument("expression")
    nxt.add_argument("--from", dest="start", default="now", help="ISO time to count from (naive times are in --tz)")
    nxt.add_argument("-n", "--count", type=int, default=5)
    nxt.add_argument("--dialect", choices=DIALECTS, default="vixie", help="vixie (cronie, Debian cron) or robfig (Kubernetes CronJob, croniter) day-field rule")
    exp = sub.add_parser("explain", parents=[common], help="expand each field and state the day rule and the DST rule")
    exp.add_argument("expression")
    exp.add_argument("--dialect", choices=DIALECTS, default="vixie")
    cal = sub.add_parser("calendar", parents=[common, policy], help="systemd OnCalendar subset, like systemd-analyze calendar")
    cal.add_argument("spec")
    cal.add_argument("--base-time", default="now")
    cal.add_argument("--iterations", type=int, default=1)
    cal.add_argument("--last-trigger", help="when the timer last fired; reports Persistent= catch-up at --base-time")
    cal.add_argument("--persistent", action="store_true")
    sim = sub.add_parser("simulate", parents=[common], help="run jobs on a virtual clock and print the run journal")
    sim.add_argument("--jobs", required=True, help="JSON list of jobs")
    sim.add_argument("--from", dest="start", required=True)
    sim.add_argument("--to", dest="end", required=True)
    sim.add_argument("--down", action="append", default=[], help="FROM/TO window in which the scheduler is not running (repeatable)")
    sim.add_argument("--seed", default="", help="mixed into the jitter hash")
    run = sub.add_parser("run", parents=[common], help="run one job with jitter, a lock and a timeout")
    run.add_argument("--name")
    guard = run.add_mutually_exclusive_group()
    guard.add_argument("--lock", help="flock(2) this file; the kernel drops the lock when the holder dies")
    guard.add_argument("--pidfile", help="O_EXCL pid file with stale-lock detection")
    run.add_argument("--stale-after", type=int, help="treat a pidfile older than this many seconds as stale")
    run.add_argument("--timeout", type=float, help="seconds before the job's process group gets SIGTERM (exit 124)")
    run.add_argument("--jitter", type=int, default=0, help="wait a stable per-host delay in [0, N) seconds first")
    run.add_argument("--journal", help="append one JSON line per run to this file")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("cmd", nargs=argparse.REMAINDER)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    out = sys.stdout
    try:
        if args.command == "next":
            zone = load_zone(args.tz)
            expr = parse_cron(args.expression, args.dialect)
            gap, fold = resolve_policy(expr, args.gap, args.fold)
            runs = next_runs(expr, zone, parse_instant(args.start, zone), args.count, gap, fold)
            if args.json:
                out.write(json.dumps({"expression": expr.text, "tz": args.tz, "dialect": args.dialect, "gap": gap, "fold": fold, "runs": [r.to_json() for r in runs]}, indent=2) + "\n")
            else:
                for run in runs:
                    note = f"  {run.note}" if run.note else ""
                    out.write(f"{run.local.isoformat()}  {run.local.strftime('%a')}  {format_utc(run.instant)}{note}\n")
            return 0
        if args.command == "explain":
            info = explain_cron(parse_cron(args.expression, args.dialect))
            if args.json:
                out.write(json.dumps(info, indent=2) + "\n")
            else:
                for key in ("expression", "dialect", "minute", "hour", "day_of_month", "month", "day_of_week"):
                    out.write(f"{key.replace('_', '-'):<14}{info[key]}\n")
                out.write(f"{'day rule':<14}{info['day_rule_text']}\n")
                if info["dialects_disagree"]:
                    out.write(f"{'warning':<14}the other dialect uses {'AND' if info['day_rule'] == 'or' else 'OR'} here; cronie and Kubernetes run this on different days\n")
                out.write(f"{'dst':<14}{info['dst']['text']}\n")
            return 0
        if args.command == "calendar":
            spec = parse_calendar(args.spec)
            zone_name = spec.zone or args.tz
            zone = load_zone(zone_name)
            gap, fold = resolve_policy(spec, args.gap, args.fold)
            base = parse_instant(args.base_time, zone)
            runs = next_runs(spec, zone, base, args.iterations, gap, fold)
            activation = None
            if args.last_trigger:
                activation = timer_activation(spec, zone, base, parse_instant(args.last_trigger, zone), args.persistent)
            if args.json:
                payload = {"original": spec.text, "normalized": spec.normalized, "tz": zone_name, "gap": gap, "fold": fold, "runs": [r.to_json() for r in runs]}
                if activation is not None:
                    payload["activation"] = activation
                out.write(json.dumps(payload, indent=2) + "\n")
            else:
                print_calendar(spec, runs, zone_name, out)
                if activation is not None:
                    verdict = "fires immediately (Persistent=true catch-up)" if activation["fires_on_activation"] else "waits for the next elapse"
                    out.write(f"     Activation: {activation['missed']} elapse(s) missed since the last trigger; {verdict}\n")
            return 0
        if args.command == "simulate":
            with open(args.jobs, encoding="utf-8") as handle:
                jobs = load_jobs(json.load(handle))
            start, end = parse_instant(args.start, UTC), parse_instant(args.end, UTC)
            if end <= start:
                raise ScheduleError("--to must be after --from")
            downtime = [parse_downtime(text, UTC) for text in args.down]
            journal = simulate(jobs, start, end, downtime, args.seed)
            summary = summarize(journal)
            if args.json:
                out.write(json.dumps({"from": format_utc(start), "to": format_utc(end), "journal": journal, "summary": summary}, indent=2) + "\n")
            else:
                for entry in journal:
                    reason = f"  ({entry['reason']})" if entry["reason"] else ""
                    out.write(f"{entry['time']}  {entry['job']:<12} {entry['event']:<6} scheduled {entry['scheduled']}{reason}\n")
                for name, counts in sorted(summary.items()):
                    out.write(f"{name}: " + ", ".join(f"{k} {v}" for k, v in counts.items()) + "\n")
            return 0
        command = args.cmd[1:] if args.cmd[:1] == ["--"] else list(args.cmd)
        if not command:
            raise ScheduleError("run needs a command after --")
        name = args.name or os.path.basename(command[0])
        return run_job(command, name, args.lock, args.pidfile, args.stale_after, args.timeout, args.jitter, args.journal, args.dry_run, args.json)
    except ScheduleError as exc:
        print(f"minicron: {exc}", file=sys.stderr)
        return 1 if "never runs" in str(exc) else 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"minicron: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
