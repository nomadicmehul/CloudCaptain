from __future__ import annotations

import calendar
import re
from typing import Dict, List, Optional, Tuple

TIMESTAMP_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$")
SINCE_RE = re.compile(r"^[0-9]+(s|m|h)$")


def parse_timestamp(text: str) -> Tuple[int, int]:
    match = TIMESTAMP_RE.match(text)
    if not match:
        raise ValueError(f"not an RFC 3339 timestamp: {text!r}")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    nanos = int((match.group(7) or "").ljust(9, "0"))
    seconds = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0))
    zone = match.group(8)
    if zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        seconds -= sign * (int(zone[1:3]) * 3600 + int(zone[4:6]) * 60)
    return seconds, nanos


def split_line(raw: str) -> Optional[Tuple[str, str]]:
    stamp, _, text = raw.partition(" ")
    if not TIMESTAMP_RE.match(stamp):
        return None
    return stamp, text


def merge(lines_by_pod: Dict[str, str]) -> List[Dict[str, str]]:
    entries = []
    for pod in sorted(lines_by_pod):
        for index, raw in enumerate(lines_by_pod[pod].splitlines()):
            parts = split_line(raw)
            if parts is None:
                continue
            stamp, text = parts
            entries.append((parse_timestamp(stamp), pod, index, {"time": stamp, "pod": pod, "line": text}))
    entries.sort(key=lambda e: e[:3])
    return [e[3] for e in entries]


def valid_since(value: str) -> bool:
    return bool(SINCE_RE.match(value))
