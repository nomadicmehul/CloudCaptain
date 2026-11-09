"""minivuln: merge Trivy and Grype JSON reports into one deduplicated list of findings, score their CVSS v3.1 and v4.0 vectors with the published formulas, enrich them with EPSS and the CISA KEV catalog, apply OpenVEX statements, rank them by a documented policy with remediation due dates, and diff two scans."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import itertools
import json
import math
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote

CVE = re.compile(r"^CVE-\d{4}-\d{4,}$")
SEVERITY_BANDS = ((9.0, "CRITICAL"), (7.0, "HIGH"), (4.0, "MEDIUM"), (0.1, "LOW"))
VECTOR_PREFIXES = {"CVSS:3.0": "3.0", "CVSS:3.1": "3.1", "CVSS:4.0": "4.0"}

V3_BASE = {"AV": "N A L P", "AC": "L H", "PR": "N L H", "UI": "N R", "S": "U C", "C": "H L N", "I": "H L N", "A": "H L N"}
V3_OTHER = {
    "E": "X U P F H", "RL": "X O T W U", "RC": "X U R C", "CR": "X L M H", "IR": "X L M H", "AR": "X L M H",
    "MAV": "X N A L P", "MAC": "X L H", "MPR": "X N L H", "MUI": "X N R", "MS": "X U C", "MC": "X N L H", "MI": "X N L H", "MA": "X N L H",
}
V3_WEIGHT = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "CIA": {"H": 0.56, "L": 0.22, "N": 0.0},
}
V3_PRIVILEGES = {"U": {"N": 0.85, "L": 0.62, "H": 0.27}, "C": {"N": 0.85, "L": 0.68, "H": 0.5}}

V4_BASE = {"AV": "N A L P", "AC": "L H", "AT": "N P", "PR": "N L H", "UI": "N P A", "VC": "H L N", "VI": "H L N", "VA": "H L N", "SC": "H L N", "SI": "H L N", "SA": "H L N"}
V4_THREAT = {"E": "X A P U"}
V4_ENVIRONMENTAL = {
    "CR": "X H M L", "IR": "X H M L", "AR": "X H M L", "MAV": "X N A L P", "MAC": "X L H", "MAT": "X N P", "MPR": "X N L H", "MUI": "X N P A",
    "MVC": "X H L N", "MVI": "X H L N", "MVA": "X H L N", "MSC": "X H L N", "MSI": "X S H L N", "MSA": "X S H L N",
}
V4_SUPPLEMENTAL = {"S": "X N P", "AU": "X N Y", "R": "X A U I", "V": "X D C", "RE": "X L M H", "U": "X Clear Green Amber Red"}
V4_MACRO_SCORES = """
10 9.9 9.8 9.5 9.5 9.2 10 9.6 9.3 8.7 9.1 8.1 9.3 9 8.9 8 8.1 6.8
9.8 9.5 9.5 9.2 9 8.4 9.3 9.2 8.9 8.1 8.1 6.5 8.8 8 7.8 7 6.9 4.8
9.2 8.2 7.2 7.9 6.9 5 6.9 5.5 2.7 9.9 9.7 9.5 9.2 9.2 8.5 9.5 9.1 9
8.3 8.4 7.1 9.2 8.1 8.2 7.1 7.2 5.3 9.5 9.3 9.2 8.5 8.5 7.3 9.2 8.2 8
7.2 7 5.9 8.4 7 7.1 5.2 5 3 8.6 7.5 5.2 7.1 5.2 2.9 6.3 2.9 1.7
9.8 9.5 9.4 8.7 9.1 8.1 9.4 8.9 8.6 7.4 7.7 6.4 8.7 7.5 7.4 6.3 6.3 4.9
9.4 8.9 8.8 7.7 7.6 6.7 8.6 7.6 7.4 5.8 5.9 5 7.2 5.7 5.7 5.2 5.2 2.5
8.3 7 5.4 6.5 5.8 2.6 5.3 2.1 1.3 9.5 9 8.8 7.6 7.6 7 9 7.7 7.5
6.2 6.1 5.3 7.7 6.6 6.8 5.9 5.2 3 8.9 7.8 7.6 6.7 6.2 5.8 7.4 5.9 5.7
5.7 4.7 2.3 6.1 5.2 5.7 2.9 2.4 1.6 7.1 5.9 3 5.8 2.6 1.5 2.3 1.3 0.6
9.3 8.7 8.6 7.2 7.5 5.8 8.6 7.4 7.4 6.1 5.6 3.4 7 5.4 5.2 4 4 2.2
8.5 7.5 7.4 5.5 6.2 5.1 7.2 5.7 5.5 4.1 4.6 1.9 5.3 3.6 3.4 1.9 1.9 0.8
6.4 5.1 2 4.7 2.1 1.1 2.4 0.9 0.4 8.8 7.5 7.3 5.3 6 5 7.3 5.5 5.9
4 4.1 2 5.4 4.3 4.5 2.2 2 1.1 7.5 5.5 5.8 4.5 4 2.1 6.1 5.1 4.8
1.8 2 0.9 4.6 1.8 1.7 0.7 0.8 0.2 5.3 2.4 1.4 2.4 1.2 0.5 1 0.3 0.1
"""
V4_MACROVECTORS = [
    "".join(map(str, eq))
    for eq in itertools.product(range(3), range(2), range(3), range(3), range(3), range(2))
    if not (eq[2] == 2 and eq[5] == 0)
]
V4_LOOKUP = dict(zip(V4_MACROVECTORS, (float(v) for v in V4_MACRO_SCORES.split())))
V4_LEVELS = {
    "AV": {"N": 0.0, "A": 0.1, "L": 0.2, "P": 0.3},
    "PR": {"N": 0.0, "L": 0.1, "H": 0.2},
    "UI": {"N": 0.0, "P": 0.1, "A": 0.2},
    "AC": {"L": 0.0, "H": 0.1},
    "AT": {"N": 0.0, "P": 0.1},
    "VC": {"H": 0.0, "L": 0.1, "N": 0.2},
    "VI": {"H": 0.0, "L": 0.1, "N": 0.2},
    "VA": {"H": 0.0, "L": 0.1, "N": 0.2},
    "SC": {"H": 0.1, "L": 0.2, "N": 0.3},
    "SI": {"S": 0.0, "H": 0.1, "L": 0.2, "N": 0.3},
    "SA": {"S": 0.0, "H": 0.1, "L": 0.2, "N": 0.3},
    "CR": {"H": 0.0, "M": 0.1, "L": 0.2},
    "IR": {"H": 0.0, "M": 0.1, "L": 0.2},
    "AR": {"H": 0.0, "M": 0.1, "L": 0.2},
}
V4_HIGHEST = {
    "eq1": {0: ["AV:N/PR:N/UI:N"], 1: ["AV:A/PR:N/UI:N", "AV:N/PR:L/UI:N", "AV:N/PR:N/UI:P"], 2: ["AV:P/PR:N/UI:N", "AV:A/PR:L/UI:P"]},
    "eq2": {0: ["AC:L/AT:N"], 1: ["AC:H/AT:N", "AC:L/AT:P"]},
    "eq3eq6": {
        (0, 0): ["VC:H/VI:H/VA:H/CR:H/IR:H/AR:H"],
        (0, 1): ["VC:H/VI:H/VA:L/CR:M/IR:M/AR:H", "VC:H/VI:H/VA:H/CR:M/IR:M/AR:M"],
        (1, 0): ["VC:L/VI:H/VA:H/CR:H/IR:H/AR:H", "VC:H/VI:L/VA:H/CR:H/IR:H/AR:H"],
        (1, 1): ["VC:L/VI:H/VA:L/CR:H/IR:M/AR:H", "VC:L/VI:H/VA:H/CR:H/IR:M/AR:M", "VC:H/VI:L/VA:H/CR:M/IR:H/AR:M", "VC:H/VI:L/VA:L/CR:M/IR:H/AR:H", "VC:L/VI:L/VA:H/CR:H/IR:H/AR:M"],
        (2, 1): ["VC:L/VI:L/VA:L/CR:H/IR:H/AR:H"],
    },
    "eq4": {0: ["SC:H/SI:S/SA:S"], 1: ["SC:H/SI:H/SA:H"], 2: ["SC:L/SI:L/SA:L"]},
}
V4_DEPTH = {
    "eq1": {0: 1, 1: 4, 2: 5},
    "eq2": {0: 1, 1: 2},
    "eq3eq6": {(0, 0): 7, (0, 1): 6, (1, 0): 8, (1, 1): 8, (2, 1): 10},
    "eq4": {0: 6, 1: 5, 2: 4},
}
V4_WORST_CASE = {"E": "A", "CR": "H", "IR": "H", "AR": "H"}
V4_ROUNDING_EPSILON = 1e-6

TRIVY_TYPES = {
    "debian": "deb", "ubuntu": "deb", "alpine": "apk", "wolfi": "apk", "chainguard": "apk",
    "redhat": "rpm", "centos": "rpm", "rocky": "rpm", "alma": "rpm", "amazon": "rpm", "oracle": "rpm", "cbl-mariner": "rpm", "azurelinux": "rpm", "photon": "rpm", "suse linux enterprise server": "rpm", "opensuse.leap": "rpm",
    "python-pkg": "pypi", "pip": "pypi", "pipenv": "pypi", "poetry": "pypi", "uv": "pypi",
    "node-pkg": "npm", "npm": "npm", "yarn": "npm", "pnpm": "npm", "bun": "npm",
    "gobinary": "golang", "gomod": "golang", "jar": "maven", "pom": "maven", "gradle": "maven", "sbt": "maven",
    "cargo": "cargo", "rustbinary": "cargo", "gemspec": "gem", "bundler": "gem", "composer": "composer", "nuget": "nuget", "dotnet-core": "nuget",
}
GRYPE_TYPES = {
    "deb": "deb", "apk": "apk", "rpm": "rpm", "python": "pypi", "npm": "npm", "go-module": "golang", "java-archive": "maven",
    "jenkins-plugin": "maven", "rust-crate": "cargo", "gem": "gem", "php-composer": "composer", "dotnet": "nuget", "binary": "generic",
}
TRIVY_STATES = {
    "fixed": "fixed", "affected": "not-fixed", "will_not_fix": "wont-fix", "fix_deferred": "deferred", "end_of_life": "eol",
    "under_investigation": "unknown", "unknown": "unknown", "not_affected": "not-affected",
}
STATE_ORDER = ("fixed", "wont-fix", "deferred", "eol", "not-fixed", "not-affected", "unknown")
SOURCE_ORDER = ("nvd", "ghsa")
KNOWN_SOURCES = {"134c704f-9b21-4f2e-91b3-4a467353bcc0": "cisa-adp", "secalert@redhat.com": "redhat"}

VEX_STATUSES = ("not_affected", "affected", "fixed", "under_investigation")
VEX_SUPPRESSING = ("not_affected", "fixed")
VEX_JUSTIFICATIONS = (
    "component_not_present",
    "vulnerable_code_not_present",
    "vulnerable_code_not_in_execute_path",
    "vulnerable_code_cannot_be_controlled_by_adversary",
    "inline_mitigations_already_exist",
)

DEFAULT_POLICY: Dict[str, Any] = {
    "name": "kev-epss-cvss",
    "exposure": {"internet": 1.0, "internal": 0.5, "isolated": 0.1},
    "tiers": [
        {"priority": "P1", "kev": True, "sla_days": 7},
        {"priority": "P2", "min_threat": 0.1, "sla_days": 30},
        {"priority": "P3", "min_cvss": 7.0, "sla_days": 90},
        {"priority": "P4", "sla_days": 180},
    ],
}


class InputError(ValueError):
    pass


def js_round(value: float) -> int:
    whole = math.floor(value)
    return whole + 1 if value - whole >= 0.5 else whole


def roundup(value: float) -> float:
    scaled = js_round(value * 100000)
    if scaled % 10000 == 0:
        return scaled / 100000.0
    return (math.floor(scaled / 10000) + 1) / 10.0


def severity_of(score: float) -> str:
    for floor, name in SEVERITY_BANDS:
        if score >= floor:
            return name
    return "NONE"


@dataclass(frozen=True)
class Score:
    vector: str
    version: str
    score: float
    severity: str
    nomenclature: str
    macrovector: str = ""


def parse_vector(text: str) -> Tuple[str, Dict[str, str]]:
    raw = str(text).strip()
    prefix, _, rest = raw.partition("/")
    if prefix not in VECTOR_PREFIXES:
        raise InputError(f"not a CVSS v3.0, v3.1 or v4.0 vector: {raw!r} (it must start with CVSS:3.1/ or CVSS:4.0/; CVSS v2 vectors have no prefix and are not scored)")
    version = VECTOR_PREFIXES[prefix]
    required = V4_BASE if version == "4.0" else V3_BASE
    optional = dict(V4_THREAT, **V4_ENVIRONMENTAL, **V4_SUPPLEMENTAL) if version == "4.0" else V3_OTHER
    metrics: Dict[str, str] = {}
    for part in rest.split("/") if rest else []:
        key, sep, value = part.partition(":")
        allowed = required.get(key) or optional.get(key)
        if not sep or allowed is None:
            raise InputError(f"{raw}: unknown metric {part!r} for CVSS {version}")
        if key in metrics:
            raise InputError(f"{raw}: metric {key} appears twice")
        if value not in allowed.split():
            raise InputError(f"{raw}: {key}:{value} is not one of {allowed.replace(' ', ', ')}")
        metrics[key] = value
    missing = [key for key in required if key not in metrics]
    if missing:
        raise InputError(f"{raw}: missing base metric(s) {', '.join(missing)}")
    return version, metrics


def v3_base_score(metrics: Dict[str, str]) -> float:
    scope = metrics["S"]
    cia = V3_WEIGHT["CIA"]
    iss = 1 - (1 - cia[metrics["C"]]) * (1 - cia[metrics["I"]]) * (1 - cia[metrics["A"]])
    if scope == "U":
        impact = 6.42 * iss
    else:
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
    exploitability = 8.22 * V3_WEIGHT["AV"][metrics["AV"]] * V3_WEIGHT["AC"][metrics["AC"]] * V3_PRIVILEGES[scope][metrics["PR"]] * V3_WEIGHT["UI"][metrics["UI"]]
    if impact <= 0:
        return 0.0
    if scope == "U":
        return roundup(min(impact + exploitability, 10))
    return roundup(min(1.08 * (impact + exploitability), 10))


def effective(metrics: Dict[str, str], key: str) -> str:
    value = metrics.get(key, "X")
    if value == "X" and key in V4_WORST_CASE:
        return V4_WORST_CASE[key]
    modified = metrics.get("M" + key, "X")
    if modified != "X":
        return modified
    return value


def v4_macrovector(metrics: Dict[str, str]) -> Tuple[int, int, int, int, int, int]:
    av, pr, ui = effective(metrics, "AV"), effective(metrics, "PR"), effective(metrics, "UI")
    if av == "N" and pr == "N" and ui == "N":
        eq1 = 0
    elif "N" in (av, pr, ui) and av != "P":
        eq1 = 1
    else:
        eq1 = 2
    eq2 = 0 if effective(metrics, "AC") == "L" and effective(metrics, "AT") == "N" else 1
    vc, vi, va = effective(metrics, "VC"), effective(metrics, "VI"), effective(metrics, "VA")
    if vc == "H" and vi == "H":
        eq3 = 0
    elif "H" in (vc, vi, va):
        eq3 = 1
    else:
        eq3 = 2
    if effective(metrics, "MSI") == "S" or effective(metrics, "MSA") == "S":
        eq4 = 0
    elif "H" in (effective(metrics, "SC"), effective(metrics, "SI"), effective(metrics, "SA")):
        eq4 = 1
    else:
        eq4 = 2
    eq5 = {"A": 0, "P": 1, "U": 2}[effective(metrics, "E")]
    requirements = ((effective(metrics, "CR"), vc), (effective(metrics, "IR"), vi), (effective(metrics, "AR"), va))
    eq6 = 0 if any(req == "H" and impact == "H" for req, impact in requirements) else 1
    return eq1, eq2, eq3, eq4, eq5, eq6


def macro_key(eqs: Sequence[int]) -> str:
    return "".join(str(e) for e in eqs)


def next_lower_scores(eqs: Tuple[int, int, int, int, int, int]) -> Dict[str, Optional[float]]:
    eq1, eq2, eq3, eq4, eq5, eq6 = eqs
    lookup = V4_LOOKUP.get
    if (eq3, eq6) in ((0, 1), (1, 1)):
        eq3eq6 = lookup(macro_key((eq1, eq2, eq3 + 1, eq4, eq5, eq6)))
    elif (eq3, eq6) == (1, 0):
        eq3eq6 = lookup(macro_key((eq1, eq2, eq3, eq4, eq5, eq6 + 1)))
    elif (eq3, eq6) == (0, 0):
        left = lookup(macro_key((eq1, eq2, eq3, eq4, eq5, eq6 + 1)))
        right = lookup(macro_key((eq1, eq2, eq3 + 1, eq4, eq5, eq6)))
        eq3eq6 = None if left is None or right is None else max(left, right)
    else:
        eq3eq6 = lookup(macro_key((eq1, eq2, eq3 + 1, eq4, eq5, eq6 + 1)))
    return {
        "eq1": lookup(macro_key((eq1 + 1, eq2, eq3, eq4, eq5, eq6))),
        "eq2": lookup(macro_key((eq1, eq2 + 1, eq3, eq4, eq5, eq6))),
        "eq3eq6": eq3eq6,
        "eq4": lookup(macro_key((eq1, eq2, eq3, eq4 + 1, eq5, eq6))),
        "eq5": lookup(macro_key((eq1, eq2, eq3, eq4, eq5 + 1, eq6))),
    }


def severity_distances(metrics: Dict[str, str], eqs: Tuple[int, int, int, int, int, int]) -> Dict[str, float]:
    eq1, eq2, eq3, eq4, _, eq6 = eqs
    distances: Dict[str, float] = {}
    for parts in itertools.product(V4_HIGHEST["eq1"][eq1], V4_HIGHEST["eq2"][eq2], V4_HIGHEST["eq3eq6"][(eq3, eq6)], V4_HIGHEST["eq4"][eq4]):
        highest = dict(item.split(":") for item in "/".join(parts).split("/"))
        distances = {key: levels[effective(metrics, key)] - levels[highest[key]] for key, levels in V4_LEVELS.items()}
        if all(d >= 0 for d in distances.values()):
            break
    return distances


def v4_score(metrics: Dict[str, str], eqs: Optional[Tuple[int, int, int, int, int, int]] = None) -> float:
    if all(effective(metrics, key) == "N" for key in ("VC", "VI", "VA", "SC", "SI", "SA")):
        return 0.0
    eqs = eqs or v4_macrovector(metrics)
    eq1, eq2, eq3, eq4, _, eq6 = eqs
    value = V4_LOOKUP[macro_key(eqs)]
    lower = next_lower_scores(eqs)
    d = severity_distances(metrics, eqs)
    current = {
        "eq1": d["AV"] + d["PR"] + d["UI"],
        "eq2": d["AC"] + d["AT"],
        "eq3eq6": d["VC"] + d["VI"] + d["VA"] + d["CR"] + d["IR"] + d["AR"],
        "eq4": d["SC"] + d["SI"] + d["SA"],
    }
    depth = {
        "eq1": V4_DEPTH["eq1"][eq1] * 0.1,
        "eq2": V4_DEPTH["eq2"][eq2] * 0.1,
        "eq3eq6": V4_DEPTH["eq3eq6"][(eq3, eq6)] * 0.1,
        "eq4": V4_DEPTH["eq4"][eq4] * 0.1,
    }
    proportional = []
    for eq in ("eq1", "eq2", "eq3eq6", "eq4"):
        if lower[eq] is not None:
            proportional.append((value - lower[eq]) * (current[eq] / depth[eq]))
    if lower["eq5"] is not None:
        proportional.append(0.0)
    mean = sum(proportional) / len(proportional) if proportional else 0.0
    result = max(0.0, min(10.0, value - mean))
    return js_round((result + V4_ROUNDING_EPSILON) * 10) / 10


def v4_nomenclature(metrics: Dict[str, str]) -> str:
    threat = any(metrics.get(key, "X") != "X" for key in V4_THREAT)
    environmental = any(metrics.get(key, "X") != "X" for key in V4_ENVIRONMENTAL)
    return "CVSS-B" + ("T" if threat else "") + ("E" if environmental else "")


def score_vector(text: str) -> Score:
    version, metrics = parse_vector(text)
    vector = str(text).strip()
    if version == "4.0":
        eqs = v4_macrovector(metrics)
        value = v4_score(metrics, eqs)
        return Score(vector, version, value, severity_of(value), v4_nomenclature(metrics), macro_key(eqs))
    value = v3_base_score(metrics)
    return Score(vector, version, value, severity_of(value), "base")


@dataclass(frozen=True)
class Purl:
    type: str
    namespace: str
    name: str
    version: str
    qualifiers: Dict[str, str]


def parse_purl(text: str) -> Optional[Purl]:
    raw = str(text or "").strip()
    if not raw.startswith("pkg:"):
        return None
    rest = raw[4:].split("#", 1)[0]
    path, _, query = rest.partition("?")
    kind, _, remainder = path.strip("/").partition("/")
    if not kind or not remainder:
        return None
    head, _, last = remainder.rpartition("/")
    name, _, version = last.partition("@")
    qualifiers = {}
    for pair in query.split("&") if query else []:
        key, _, value = pair.partition("=")
        if key:
            qualifiers[key.lower()] = unquote(value)
    namespace = "/".join(unquote(part) for part in head.split("/") if part)
    return Purl(kind.lower(), namespace, unquote(name), unquote(version), qualifiers)


def purl_matches(general: str, specific: str) -> bool:
    a, b = parse_purl(general), parse_purl(specific)
    if a is None or b is None:
        return False
    if (a.type, a.namespace, a.name) != (b.type, b.namespace, b.name):
        return False
    if a.version and a.version != b.version:
        return False
    return all(b.qualifiers.get(key) == value for key, value in a.qualifiers.items())


def identifier_matches(general: str, specific: str) -> bool:
    if not general or not specific:
        return False
    if general == specific:
        return True
    return general.startswith("pkg:") and specific.startswith("pkg:") and purl_matches(general, specific)


def normalise_name(ecosystem: str, name: str) -> str:
    if ecosystem == "pypi":
        return re.sub(r"[-_.]+", "-", name).lower()
    return name


def package_identity(ecosystem: str, purl: Optional[Purl], fallback: str) -> str:
    if purl is not None and purl.name:
        if purl.type == "maven" and purl.namespace:
            return f"{purl.namespace}:{purl.name}"
        if purl.type in ("npm", "golang", "composer", "swift", "github") and purl.namespace:
            return f"{purl.namespace}/{purl.name}"
        return normalise_name(ecosystem, purl.name)
    return normalise_name(ecosystem, fallback)


def canonical_id(primary: str, others: Iterable[str]) -> str:
    ordered = [primary] + sorted(set(others) - {primary})
    for candidate in ordered:
        if CVE.match(candidate):
            return candidate
    return primary


def normal_source(source: str) -> str:
    lowered = (source or "unknown").lower()
    if lowered in KNOWN_SOURCES:
        return KNOWN_SOURCES[lowered]
    if "nvd" in lowered:
        return "nvd"
    if "github" in lowered or lowered == "ghsa":
        return "ghsa"
    return lowered


def date_of(text: Any) -> str:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(text or ""))
    return match.group(1) if match else ""


def parse_date(text: str, what: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(text)[:10])
    except ValueError:
        raise InputError(f"{what}: {text!r} is not a date (use YYYY-MM-DD)")


@dataclass(frozen=True)
class CvssClaim:
    source: str
    tool: str
    vector: str
    reported: Optional[float]


@dataclass
class Finding:
    vuln: str
    ids: Set[str]
    ecosystem: str
    package: str
    version: str
    purls: Set[str] = field(default_factory=set)
    advisories: Set[str] = field(default_factory=set)
    upstream: str = ""
    fixed: Set[str] = field(default_factory=set)
    state: str = "unknown"
    severities: Dict[str, str] = field(default_factory=dict)
    claims: List[CvssClaim] = field(default_factory=list)
    tools: Set[str] = field(default_factory=set)
    locations: Set[str] = field(default_factory=set)
    first_seen: str = ""

    @property
    def aliases(self) -> List[str]:
        return sorted(self.ids - {self.vuln})

    @property
    def label(self) -> str:
        return f"{self.ecosystem}/{self.package}"

    def absorb(self, other: "Finding") -> None:
        self.ids |= other.ids
        primary = self.vuln if CVE.match(self.vuln) or not CVE.match(other.vuln) else other.vuln
        self.vuln = canonical_id(primary, self.ids)
        self.purls |= other.purls
        self.advisories |= other.advisories
        self.upstream = self.upstream or other.upstream
        self.fixed |= other.fixed
        self.state = min((self.state, other.state), key=lambda s: STATE_ORDER.index(s) if s in STATE_ORDER else len(STATE_ORDER))
        for tool, severity in other.severities.items():
            self.severities.setdefault(tool, severity)
        for claim in other.claims:
            if claim not in self.claims:
                self.claims.append(claim)
        self.tools |= other.tools
        self.locations |= other.locations
        if other.first_seen and (not self.first_seen or other.first_seen < self.first_seen):
            self.first_seen = other.first_seen


@dataclass
class Report:
    tool: str
    path: str
    artifact: str
    created: str
    identities: Set[str]
    findings: List[Finding]


def read_bytes(path: str) -> bytes:
    try:
        if path == "-":
            return sys.stdin.buffer.read()
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise InputError(f"{path}: {exc.strerror or exc}")


def read_json(path: str) -> Any:
    raw = read_bytes(path)
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{path}: not valid JSON ({exc})")


def oci_identities(references: Iterable[str]) -> Set[str]:
    found: Set[str] = set()
    for ref in references:
        repo, at, digest = ref.partition("@")
        if at and digest.startswith("sha256:"):
            found.add(f"pkg:oci/{repo.rsplit('/', 1)[-1].split(':')[0]}@{digest}")
            found.add(digest)
    return found


def trivy_report(doc: Dict[str, Any], path: str) -> Report:
    meta = doc.get("Metadata") or {}
    artifact = str(doc.get("ArtifactName") or path)
    references = [artifact] + list(meta.get("RepoTags") or []) + list(meta.get("RepoDigests") or [])
    identities = set(references) | oci_identities(references)
    created = date_of(doc.get("CreatedAt"))
    findings = []
    for result in doc.get("Results") or []:
        target_type = str(result.get("Type") or "").lower()
        for vuln in result.get("Vulnerabilities") or []:
            purl_text = str((vuln.get("PkgIdentifier") or {}).get("PURL") or "")
            purl = parse_purl(purl_text)
            ecosystem = purl.type if purl else TRIVY_TYPES.get(target_type, target_type or "unknown")
            primary = str(vuln.get("VulnerabilityID") or "")
            if not primary or not vuln.get("PkgName"):
                raise InputError(f"{path}: a Trivy vulnerability without VulnerabilityID or PkgName: {json.dumps(vuln)[:120]}")
            ids = {primary}
            claims = []
            for source, data in sorted((vuln.get("CVSS") or {}).items()):
                for vector_key, score_key in (("V3Vector", "V3Score"), ("V40Vector", "V40Score")):
                    if data.get(vector_key):
                        claims.append(CvssClaim(normal_source(source), "trivy", str(data[vector_key]), data.get(score_key)))
            fixed = {v.strip() for v in str(vuln.get("FixedVersion") or "").split(",") if v.strip()}
            findings.append(Finding(
                vuln=canonical_id(primary, ids),
                ids=ids,
                ecosystem=ecosystem,
                package=package_identity(ecosystem, purl, str(vuln["PkgName"])),
                version=str(vuln.get("InstalledVersion") or (purl.version if purl else "")),
                purls={purl_text} if purl_text else set(),
                advisories={str(v) for v in vuln.get("VendorIDs") or []},
                fixed=fixed,
                state="fixed" if fixed else TRIVY_STATES.get(str(vuln.get("Status") or "unknown"), "unknown"),
                severities={"trivy": str(vuln.get("Severity") or "UNKNOWN").upper()},
                claims=claims,
                tools={"trivy"},
                locations={str(vuln.get("PkgPath") or result.get("Target") or "")} - {""},
                first_seen=created,
            ))
    return Report("trivy", path, artifact, created, identities, findings)


def grype_report(doc: Dict[str, Any], path: str) -> Report:
    source = doc.get("source") or {}
    target = source.get("target") or {}
    if isinstance(target, dict):
        artifact = str(target.get("userInput") or path)
        references = [artifact] + list(target.get("tags") or []) + list(target.get("repoDigests") or [])
    else:
        artifact = str(target or path)
        references = [artifact]
    identities = set(references) | oci_identities(references)
    created = date_of((doc.get("descriptor") or {}).get("timestamp"))
    findings = []
    for match in doc.get("matches") or []:
        vuln = match.get("vulnerability") or {}
        art = match.get("artifact") or {}
        primary = str(vuln.get("id") or "")
        if not primary or not art.get("name"):
            raise InputError(f"{path}: a Grype match without vulnerability.id or artifact.name")
        related = match.get("relatedVulnerabilities") or []
        ids = {primary} | {str(r.get("id")) for r in related if r.get("id")}
        purl_text = str(art.get("purl") or "")
        purl = parse_purl(purl_text)
        ecosystem = purl.type if purl else GRYPE_TYPES.get(str(art.get("type") or ""), str(art.get("type") or "unknown"))
        claims = []
        for meta in [vuln] + list(related):
            for entry in meta.get("cvss") or []:
                vector = str(entry.get("vector") or "")
                if vector.startswith("CVSS:3") or vector.startswith("CVSS:4"):
                    claims.append(CvssClaim(normal_source(str(entry.get("source") or meta.get("namespace") or "")), "grype", vector, (entry.get("metrics") or {}).get("baseScore")))
        fix = vuln.get("fix") or {}
        fixed = {str(v) for v in fix.get("versions") or []}
        upstreams = art.get("upstreams") or []
        findings.append(Finding(
            vuln=canonical_id(primary, ids),
            ids=ids,
            ecosystem=ecosystem,
            package=package_identity(ecosystem, purl, str(art["name"])),
            version=str(art.get("version") or ""),
            purls={purl_text} if purl_text else set(),
            upstream=str(upstreams[0].get("name") or "") if upstreams else "",
            fixed=fixed,
            state="fixed" if fixed else str(fix.get("state") or "unknown"),
            severities={"grype": str(vuln.get("severity") or "Unknown").upper()},
            claims=list(dict.fromkeys(claims)),
            tools={"grype"},
            locations={str(loc.get("path")) for loc in art.get("locations") or [] if loc.get("path")},
            first_seen=created,
        ))
    return Report("grype", path, artifact, created, identities, findings)


def load_report(path: str) -> Report:
    doc = read_json(path)
    if isinstance(doc, dict) and "SchemaVersion" in doc and ("Results" in doc or "ArtifactName" in doc):
        return trivy_report(doc, path)
    if isinstance(doc, dict) and "matches" in doc and "descriptor" in doc:
        return grype_report(doc, path)
    raise InputError(f"{path}: neither a Trivy JSON report (trivy image --format json) nor a Grype JSON report (grype IMAGE -o json)")


def merge(findings: Iterable[Finding]) -> List[Finding]:
    merged: List[Finding] = []
    index: Dict[Tuple[str, str, str, str], Finding] = {}
    for finding in findings:
        where = (finding.ecosystem, finding.package, finding.version)
        hit = next((index[where + (i,)] for i in sorted(finding.ids) if where + (i,) in index), None)
        if hit is None:
            hit = Finding(finding.vuln, set(), finding.ecosystem, finding.package, finding.version)
            hit.state = finding.state
            hit.first_seen = finding.first_seen
            merged.append(hit)
        hit.absorb(finding)
        for i in hit.ids:
            index[where + (i,)] = hit
    return sorted(merged, key=lambda f: (f.vuln, f.ecosystem, f.package, f.version))


@dataclass(frozen=True)
class Kev:
    cve: str
    vendor: str
    product: str
    name: str
    added: str
    due: str
    ransomware: str


@dataclass
class KevCatalog:
    version: str
    released: str
    entries: Dict[str, Kev]


def load_kev(path: str) -> KevCatalog:
    doc = read_json(path)
    items = doc.get("vulnerabilities") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise InputError(f"{path}: not a CISA KEV catalog (expected a 'vulnerabilities' list as in known_exploited_vulnerabilities.json)")
    entries = {}
    for item in items:
        cve = str(item.get("cveID") or "")
        if not CVE.match(cve):
            raise InputError(f"{path}: KEV entry without a valid cveID: {json.dumps(item)[:100]}")
        entries[cve] = Kev(cve, str(item.get("vendorProject") or ""), str(item.get("product") or ""), str(item.get("vulnerabilityName") or ""),
                           str(item.get("dateAdded") or ""), str(item.get("dueDate") or ""), str(item.get("knownRansomwareCampaignUse") or "Unknown"))
    return KevCatalog(str(doc.get("catalogVersion") or ""), str(doc.get("dateReleased") or ""), entries)


@dataclass(frozen=True)
class Epss:
    cve: str
    probability: float
    percentile: float


@dataclass
class EpssFeed:
    model: str
    date: str
    scores: Dict[str, Epss]


def epss_entry(path: str, cve: Any, probability: Any, percentile: Any) -> Epss:
    try:
        value, rank = float(probability), float(percentile)
    except (TypeError, ValueError):
        raise InputError(f"{path}: EPSS row for {cve} has a non-numeric score {probability!r}")
    if not 0.0 <= value <= 1.0 or not 0.0 <= rank <= 1.0:
        raise InputError(f"{path}: EPSS score for {cve} must be a probability between 0 and 1, got {probability}")
    return Epss(str(cve), value, rank)


def load_epss(path: str) -> EpssFeed:
    raw = read_bytes(path)
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8-sig")
    if text.lstrip().startswith("{"):
        doc = json.loads(text)
        rows = doc.get("data")
        if not isinstance(rows, list):
            raise InputError(f"{path}: EPSS API JSON without a 'data' list")
        scores = {str(r["cve"]): epss_entry(path, r.get("cve"), r.get("epss"), r.get("percentile")) for r in rows}
        dates = sorted({str(r.get("date") or "") for r in rows} - {""})
        return EpssFeed("api", dates[-1] if dates else "", scores)
    model = date = ""
    body = []
    for line in text.splitlines():
        if line.startswith("#"):
            for part in line[1:].split(","):
                key, _, value = part.partition(":")
                if key.strip() == "model_version":
                    model = value.strip()
                elif key.strip() == "score_date":
                    date = value.strip()
        elif line.strip():
            body.append(line)
    reader = csv.DictReader(io.StringIO("\n".join(body)))
    if not reader.fieldnames or not {"cve", "epss", "percentile"} <= set(reader.fieldnames):
        raise InputError(f"{path}: not an EPSS CSV (expected the header cve,epss,percentile)")
    scores = {row["cve"]: epss_entry(path, row["cve"], row["epss"], row["percentile"]) for row in reader}
    return EpssFeed(model, date, scores)


@dataclass(frozen=True)
class Product:
    id: str
    subcomponents: Tuple[str, ...]


@dataclass(frozen=True)
class Statement:
    vulnerability: str
    aliases: Tuple[str, ...]
    products: Tuple[Product, ...]
    status: str
    justification: str
    impact: str
    action: str
    timestamp: str
    document: str
    author: str


def component_ids(item: Any) -> List[str]:
    if isinstance(item, str):
        return [item]
    found = [str(item.get("@id") or "")]
    found += [str(v) for v in (item.get("identifiers") or {}).values()]
    return [f for f in found if f]


def load_vex(path: str) -> Tuple[List[Statement], List[str]]:
    doc = read_json(path)
    if not isinstance(doc, dict) or not str(doc.get("@context", "")).startswith("https://openvex.dev/ns"):
        raise InputError(f"{path}: not an OpenVEX document (@context must start with https://openvex.dev/ns)")
    problems = []
    for key in ("@id", "author", "timestamp"):
        if not doc.get(key):
            problems.append(f"{path}: the document has no {key}")
    statements = []
    for number, raw in enumerate(doc.get("statements") or [], 1):
        where = f"{path} statement {number}"
        vuln = raw.get("vulnerability")
        name = str(vuln.get("name") or vuln.get("@id") or "") if isinstance(vuln, dict) else str(vuln or "")
        aliases = tuple(str(a) for a in vuln.get("aliases") or []) if isinstance(vuln, dict) else ()
        products = []
        for item in raw.get("products") or []:
            subs = tuple(s for sub in (item.get("subcomponents") or [] if isinstance(item, dict) else []) for s in component_ids(sub))
            for pid in component_ids(item):
                products.append(Product(pid, subs))
        status = str(raw.get("status") or "")
        justification = str(raw.get("justification") or "")
        impact = str(raw.get("impact_statement") or "")
        action = str(raw.get("action_statement") or "")
        if not name:
            problems.append(f"{where}: no vulnerability name")
        if not products:
            problems.append(f"{where}: no products, so it applies to nothing")
        if status not in VEX_STATUSES:
            problems.append(f"{where}: status {status!r} is not one of {', '.join(VEX_STATUSES)}")
        if status == "not_affected" and not (justification or impact):
            problems.append(f"{where}: not_affected needs a justification or an impact_statement")
        if justification and justification not in VEX_JUSTIFICATIONS:
            problems.append(f"{where}: justification {justification!r} is not one of {', '.join(VEX_JUSTIFICATIONS)}")
        if status == "affected" and not action:
            problems.append(f"{where}: affected needs an action_statement")
        statements.append(Statement(name, aliases, tuple(products), status, justification, impact, action,
                                    str(raw.get("timestamp") or doc.get("timestamp") or ""), str(doc.get("@id") or path), str(doc.get("author") or "")))
    return statements, problems


def statement_applies(statement: Statement, finding: Finding, artifact_ids: Iterable[str]) -> bool:
    if statement.vulnerability not in finding.ids and not finding.ids & set(statement.aliases):
        return False
    artifacts = list(artifact_ids)
    for product in statement.products:
        if any(identifier_matches(product.id, purl) for purl in finding.purls):
            return True
        if any(identifier_matches(product.id, a) for a in artifacts):
            if not product.subcomponents:
                return True
            if any(identifier_matches(sub, purl) for sub in product.subcomponents for purl in finding.purls):
                return True
    return False


def vex_for(finding: Finding, statements: Sequence[Statement], artifact_ids: Iterable[str]) -> Optional[Statement]:
    artifacts = list(artifact_ids)
    hits = [(s.timestamp, n, s) for n, s in enumerate(statements) if statement_applies(s, finding, artifacts)]
    return max(hits)[2] if hits else None


@dataclass(frozen=True)
class Tier:
    priority: str
    sla_days: int
    kev: bool = False
    min_threat: Optional[float] = None
    min_cvss: Optional[float] = None

    def describe(self) -> str:
        conditions = []
        if self.kev:
            conditions.append("in KEV")
        if self.min_threat is not None:
            conditions.append(f"EPSS x exposure >= {self.min_threat:g}")
        if self.min_cvss is not None:
            conditions.append(f"CVSS >= {self.min_cvss:g}")
        return f"{self.priority} = {' and '.join(conditions) or 'the rest'}, {self.sla_days} d"


@dataclass
class Policy:
    name: str
    exposure: Dict[str, float]
    tiers: List[Tier]


def load_policy(doc: Dict[str, Any], where: str = "policy") -> Policy:
    exposure = doc.get("exposure")
    tiers = doc.get("tiers")
    if not isinstance(exposure, dict) or not exposure or not isinstance(tiers, list) or not tiers:
        raise InputError(f"{where}: a policy needs an 'exposure' map and a non-empty 'tiers' list")
    parsed = []
    for raw in tiers:
        try:
            parsed.append(Tier(str(raw["priority"]), int(raw["sla_days"]), bool(raw.get("kev", False)),
                               None if raw.get("min_threat") is None else float(raw["min_threat"]),
                               None if raw.get("min_cvss") is None else float(raw["min_cvss"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise InputError(f"{where}: bad tier {json.dumps(raw)} ({exc}); each tier needs priority and sla_days")
    last = parsed[-1]
    if last.kev or last.min_threat is not None or last.min_cvss is not None:
        raise InputError(f"{where}: the last tier must have no conditions, so every finding gets a priority")
    return Policy(str(doc.get("name") or "custom"), {str(k): float(v) for k, v in exposure.items()}, parsed)


@dataclass
class Context:
    policy: Policy
    exposure: str
    now: dt.date
    kev: Optional[KevCatalog] = None
    epss: Optional[EpssFeed] = None
    statements: List[Statement] = field(default_factory=list)
    artifact_ids: Set[str] = field(default_factory=set)


@dataclass
class Assessment:
    finding: Finding
    score: Optional[Score]
    score_source: str
    v4: Optional[Score]
    kev: Optional[Kev]
    epss: Optional[Epss]
    threat: float
    priority: str
    sla_days: int
    due: str
    overdue_days: int
    vex: Optional[Statement]
    reasons: List[str]

    @property
    def suppressed(self) -> bool:
        return self.vex is not None and self.vex.status in VEX_SUPPRESSING

    @property
    def cvss(self) -> float:
        if self.score is not None:
            return self.score.score
        return self.v4.score if self.v4 is not None else 0.0


def scored_claims(finding: Finding) -> List[Tuple[CvssClaim, Optional[Score], str]]:
    results = []
    for claim in finding.claims:
        try:
            results.append((claim, score_vector(claim.vector), ""))
        except InputError as exc:
            results.append((claim, None, str(exc)))
    return results


def preferred(scored: List[Tuple[CvssClaim, Optional[Score], str]], major: str) -> Tuple[Optional[Score], str]:
    usable = [(claim, score) for claim, score, _ in scored if score is not None and score.version.startswith(major)]
    if not usable:
        return None, ""
    rank = lambda item: (SOURCE_ORDER.index(item[0].source) if item[0].source in SOURCE_ORDER else len(SOURCE_ORDER), -item[1].score, item[0].vector)
    claim, score = min(usable, key=rank)
    return score, claim.source


def assess(finding: Finding, ctx: Context) -> Assessment:
    scored = scored_claims(finding)
    score, source = preferred(scored, "3")
    v4, v4_source = preferred(scored, "4")
    cves = sorted(i for i in finding.ids if CVE.match(i))
    kev = next((ctx.kev.entries[c] for c in cves if ctx.kev and c in ctx.kev.entries), None)
    epss_hits = [ctx.epss.scores[c] for c in cves if ctx.epss and c in ctx.epss.scores]
    epss = max(epss_hits, key=lambda e: e.probability) if epss_hits else None
    weight = ctx.policy.exposure[ctx.exposure]
    threat = round((epss.probability if epss else 0.0) * weight, 5)
    cvss = score.score if score else (v4.score if v4 else 0.0)
    tier = next(t for t in ctx.policy.tiers if (not t.kev or kev) and (t.min_threat is None or threat >= t.min_threat) and (t.min_cvss is None or cvss >= t.min_cvss))
    first_seen = parse_date(finding.first_seen, "first seen") if finding.first_seen else ctx.now
    due = first_seen + dt.timedelta(days=tier.sla_days)
    reasons = []
    if kev:
        ransomware = ", known ransomware use" if kev.ransomware.lower() == "known" else ""
        product = kev.product if kev.product.startswith(kev.vendor) else f"{kev.vendor} {kev.product}"
        reasons.append(f"KEV since {kev.added} ({product}){ransomware}; CISA due date {kev.due}")
    if epss:
        reasons.append(f"EPSS {epss.probability:.3f} (percentile {epss.percentile:.3f}) x {ctx.exposure} {weight:g} = threat {threat:.3f}")
    elif ctx.epss is not None:
        reasons.append("no EPSS score: " + ("not a CVE id" if not cves else "not in the EPSS feed"))
    if score:
        reasons.append(f"CVSS {score.score} {score.severity} from {source} {score.vector}")
    if v4:
        reasons.append(f"CVSS {v4.score} {v4.severity} ({v4.nomenclature}) from {v4_source} {v4.vector}")
    if not finding.fixed:
        reasons.append(f"no fixed version ({finding.state}): mitigate, replace the package or accept with an expiry")
    vex = vex_for(finding, ctx.statements, ctx.artifact_ids)
    if vex:
        detail = vex.justification or vex.impact or vex.action
        reasons.append(f"VEX {vex.status}{': ' + detail if detail else ''} ({vex.author}, {vex.timestamp[:10]})")
        if kev and vex.status in VEX_SUPPRESSING:
            reasons.append("suppressed although it is in KEV: have a second person review the justification")
    return Assessment(finding, score, source, v4, kev, epss, threat, tier.priority, tier.sla_days, due.isoformat(),
                      max(0, (ctx.now - due).days), vex, reasons)


def rank(findings: Iterable[Finding], ctx: Context) -> Tuple[List[Assessment], List[Assessment]]:
    order = {t.priority: n for n, t in enumerate(ctx.policy.tiers)}
    assessed = [assess(f, ctx) for f in findings]
    active = [a for a in assessed if not a.suppressed]
    active.sort(key=lambda a: (order[a.priority], -a.threat, -a.cvss, not a.finding.fixed, a.finding.ecosystem, a.finding.package, a.finding.vuln, a.finding.version))
    suppressed = sorted((a for a in assessed if a.suppressed), key=lambda a: (a.finding.vuln, a.finding.package))
    return active, suppressed


def diff(old: Sequence[Finding], new: Sequence[Finding]) -> Tuple[List[Finding], List[Tuple[Finding, Finding]], List[Finding]]:
    index: Dict[Tuple[str, str, str], Finding] = {}
    for finding in old:
        for i in finding.ids:
            index.setdefault((finding.ecosystem, finding.package, i), finding)
    seen: Set[int] = set()
    still: List[Tuple[Finding, Finding]] = []
    added: List[Finding] = []
    for finding in new:
        hit = next((index[(finding.ecosystem, finding.package, i)] for i in sorted(finding.ids) if (finding.ecosystem, finding.package, i) in index), None)
        if hit is None:
            added.append(finding)
        else:
            still.append((hit, finding))
            seen.add(id(hit))
    fixed = [f for f in old if id(f) not in seen]
    return added, still, fixed


def finding_json(finding: Finding) -> Dict[str, Any]:
    claims = []
    for claim, score, error in scored_claims(finding):
        item: Dict[str, Any] = {"source": claim.source, "tool": claim.tool, "vector": claim.vector, "reported": claim.reported,
                                "computed": score.score if score else None, "version": score.version if score else None}
        if error:
            item["error"] = error
        claims.append(item)
    return {
        "vuln": finding.vuln, "aliases": finding.aliases, "ecosystem": finding.ecosystem, "package": finding.package, "version": finding.version,
        "purls": sorted(finding.purls), "advisories": sorted(finding.advisories), "upstream": finding.upstream, "fixed": sorted(finding.fixed), "state": finding.state,
        "tools": sorted(finding.tools), "severities": finding.severities, "cvss": claims, "locations": sorted(finding.locations), "first_seen": finding.first_seen,
    }


def assessment_json(a: Assessment) -> Dict[str, Any]:
    item = finding_json(a.finding)
    item.update({
        "priority": a.priority, "sla_days": a.sla_days, "due": a.due, "overdue_days": a.overdue_days, "threat": a.threat,
        "score": None if a.score is None else {"value": a.score.score, "severity": a.score.severity, "vector": a.score.vector, "source": a.score_source},
        "cvss4": None if a.v4 is None else {"value": a.v4.score, "severity": a.v4.severity, "vector": a.v4.vector, "nomenclature": a.v4.nomenclature, "macrovector": a.v4.macrovector},
        "kev": None if a.kev is None else {"added": a.kev.added, "due": a.kev.due, "vendor": a.kev.vendor, "product": a.kev.product, "ransomware": a.kev.ransomware},
        "epss": None if a.epss is None else {"probability": a.epss.probability, "percentile": a.epss.percentile},
        "vex": None if a.vex is None else {"status": a.vex.status, "justification": a.vex.justification, "impact_statement": a.vex.impact,
                                            "action_statement": a.vex.action, "author": a.vex.author, "timestamp": a.vex.timestamp, "document": a.vex.document},
        "reasons": a.reasons,
    })
    return item


def short(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "~"


def table_rows(assessments: Sequence[Assessment], start: int = 1) -> List[str]:
    lines = [f"  {'#':>3}  {'prio':4}  {'due':10}  {'vuln':16}  {'package':22}  {'installed':18}  {'fixed in':22}  {'kev':3}  {'epss':>5}  {'threat':>6}  {'cvss':>4}"]
    for n, a in enumerate(assessments, start):
        f = a.finding
        epss = f"{a.epss.probability:.3f}" if a.epss else "-"
        overdue = f" (+{a.overdue_days}d)" if a.overdue_days else ""
        lines.append(f"  {n:>3}  {a.priority:4}  {a.due:10}  {short(f.vuln, 16):16}  {short(f.label, 22):22}  {short(f.version, 18):18}  "
                     f"{short(', '.join(sorted(f.fixed)) or f.state, 22):22}  {'KEV' if a.kev else '-':3}  {epss:>5}  {a.threat:>6.3f}  {a.cvss:>4}{overdue}")
    return lines


def feeds_line(ctx: Context) -> str:
    parts = []
    if ctx.kev:
        parts.append(f"KEV {ctx.kev.version or '?'} ({len(ctx.kev.entries)} entries)")
    if ctx.epss:
        parts.append(f"EPSS {ctx.epss.date[:10] or '?'} model {ctx.epss.model or '?'} ({len(ctx.epss.scores)} scores)")
    parts.append(f"VEX {len(ctx.statements)} statement(s)")
    return ", ".join(parts)


def summary_of(active: Sequence[Assessment], suppressed: Sequence[Assessment], policy: Policy) -> Dict[str, int]:
    counts = {t.priority: sum(1 for a in active if a.priority == t.priority) for t in policy.tiers}
    counts["suppressed"] = len(suppressed)
    counts["overdue"] = sum(1 for a in active if a.overdue_days)
    return counts


def render_rank(reports: Sequence[Report], active: Sequence[Assessment], suppressed: Sequence[Assessment], ctx: Context, top: Optional[int]) -> str:
    artifacts = sorted({r.artifact for r in reports})
    tools = sorted({r.tool for r in reports})
    lines = [f"minivuln rank: {', '.join(artifacts)} ({', '.join(tools)}), exposure {ctx.exposure}, now {ctx.now.isoformat()}",
             "policy " + ctx.policy.name + ": " + "; ".join(t.describe() for t in ctx.policy.tiers),
             feeds_line(ctx)]
    shown = list(active[:top] if top else active)
    lines += table_rows(shown)
    if len(shown) < len(active):
        lines.append(f"  ... {len(active) - len(shown)} more")
    if shown:
        lines.append(f"why #1: " + "; ".join(shown[0].reasons))
    if suppressed:
        lines.append(f"suppressed by VEX ({len(suppressed)}):")
        for a in suppressed:
            assert a.vex is not None
            lines.append(f"  {a.finding.vuln:16}  {a.finding.label:22}  {a.vex.status}: {a.vex.justification or a.vex.impact} ({a.vex.author}, {a.vex.timestamp[:10]})")
    counts = summary_of(active, suppressed, ctx.policy)
    lines.append("summary: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return "\n".join(lines)


def render_ingest(reports: Sequence[Report], merged: Sequence[Finding]) -> str:
    lines = [f"{r.tool:6} {r.artifact}  {r.created or '?'}  {len(r.findings)} findings" for r in reports]
    both = sum(1 for f in merged if len(f.tools) > 1)
    only = {tool: sum(1 for f in merged if f.tools == {tool}) for tool in sorted({r.tool for r in reports})}
    lines.append(f"merged {len(merged)} findings: {both} from more than one tool, " + ", ".join(f"{n} only {t}" for t, n in only.items()))
    for f in merged:
        scored = scored_claims(f)
        score, source = preferred(scored, "3")
        cvss = f"{source} {score.score} {score.vector}" if score else "no CVSS v3"
        mismatches = [f"{c.tool}/{c.source} reported {c.reported}, computed {s.score}" for c, s, _ in scored if s and c.reported is not None and float(c.reported) != s.score]
        alias = f" ({', '.join(f.aliases)})" if f.aliases else ""
        lines.append(f"  {f.vuln}{alias}  {f.label} {f.version}  fixed {', '.join(sorted(f.fixed)) or f.state}  [{' '.join(sorted(f.tools))}]  {cvss}"
                     + (f"  MISMATCH {'; '.join(mismatches)}" if mismatches else ""))
    return "\n".join(lines)


def tool_warning(old: Sequence[Report], new: Sequence[Report]) -> str:
    before, after = sorted({r.tool for r in old}), sorted({r.tool for r in new})
    if before == after:
        return ""
    return f"warning: the old side was scanned with {', '.join(before)} and the new side with {', '.join(after)}; a finding only one tool reports will look fixed or new"


def render_diff(old: Sequence[Report], new: Sequence[Report], added: Sequence[Assessment], still: Sequence[Assessment], fixed: Sequence[Finding],
                suppressed: Sequence[Assessment] = ()) -> str:
    def describe(reports: Sequence[Report]) -> str:
        return ", ".join(sorted({f"{r.artifact} ({r.created or '?'})" for r in reports}))

    lines = [f"minivuln diff: {describe(old)} -> {describe(new)}"]
    warning = tool_warning(old, new)
    if warning:
        lines.append(warning)
    lines.append(f"fixed ({len(fixed)}):")
    lines += [f"  - {f.vuln:16}  {f.label:22}  {f.version}" for f in fixed]
    lines.append(f"new ({len(added)}):")
    lines += [f"  + {a.priority}  {a.finding.vuln:16}  {a.finding.label:22}  {a.finding.version}  due {a.due}" for a in added]
    lines.append(f"still open ({len(still)}):")
    lines += [f"  = {a.priority}  {a.finding.vuln:16}  {a.finding.label:22}  {a.finding.version}  since {a.finding.first_seen}, due {a.due}"
              + (f", overdue {a.overdue_days}d" if a.overdue_days else "") for a in still]
    if suppressed:
        lines.append(f"suppressed by VEX ({len(suppressed)}):")
        lines += [f"  ~ {a.finding.vuln:16}  {a.finding.label:22}  {a.vex.status if a.vex else ''}" for a in suppressed]
    return "\n".join(lines)


def add_enrichment(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kev", help="CISA KEV catalog JSON (known_exploited_vulnerabilities.json)")
    parser.add_argument("--epss", help="EPSS scores: the daily CSV (optionally .gz) or the api.first.org JSON")
    parser.add_argument("--vex", action="append", default=[], help="OpenVEX document; repeatable")
    parser.add_argument("--product", action="append", default=[], help="extra identifier of the scanned artifact for VEX product matching, e.g. pkg:oci/api@sha256:...")
    parser.add_argument("--exposure", default="internal", help="exposure of the workload: a key of the policy's exposure map (default policy: internet, internal, isolated)")
    parser.add_argument("--policy", help="policy JSON with 'exposure' weights and 'tiers' (default: KEV, then EPSS x exposure, then CVSS)")
    parser.add_argument("--now", help="evaluation date YYYY-MM-DD (default: today, UTC)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minivuln", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("cvss", help="score CVSS v3.0, v3.1 and v4.0 vectors")
    p.add_argument("vectors", nargs="*")
    p.add_argument("--file", help="one vector per line; text after ' - ' is ignored, so published test-vector files work as they are")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("ingest", help="normalise and deduplicate Trivy and Grype JSON reports")
    p.add_argument("reports", nargs="+")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("rank", help="rank findings by the policy and assign due dates")
    p.add_argument("reports", nargs="+")
    p.add_argument("--top", type=int, help="show only the first N rows in text output")
    p.add_argument("--fail-on", help="exit 1 if any unsuppressed finding has this priority or a higher one, e.g. P1")
    add_enrichment(p)
    p = sub.add_parser("diff", help="compare two scans: new, fixed and still open")
    p.add_argument("--old", nargs="+", required=True)
    p.add_argument("--new", nargs="+", required=True)
    add_enrichment(p)
    p = sub.add_parser("vex", help="list and check OpenVEX statements")
    p.add_argument("files", nargs="+")
    p.add_argument("--json", action="store_true")
    return parser


def context_from(args: argparse.Namespace, reports: Sequence[Report]) -> Context:
    policy = load_policy(read_json(args.policy), args.policy) if args.policy else load_policy(DEFAULT_POLICY, "default policy")
    if args.exposure not in policy.exposure:
        raise InputError(f"--exposure {args.exposure!r} is not in the policy's exposure map ({', '.join(policy.exposure)})")
    now = parse_date(args.now, "--now") if args.now else dt.datetime.now(dt.timezone.utc).date()
    statements: List[Statement] = []
    problems: List[str] = []
    for path in args.vex:
        found, issues = load_vex(path)
        statements += found
        problems += issues
    if problems:
        raise InputError("invalid VEX, refusing to suppress anything:\n  " + "\n  ".join(problems))
    artifact_ids = set(args.product)
    for report in reports:
        artifact_ids |= report.identities
    return Context(policy, args.exposure, now, load_kev(args.kev) if args.kev else None, load_epss(args.epss) if args.epss else None, statements, artifact_ids)


def priority_reached(active: Sequence[Assessment], threshold: str, policy: Policy) -> bool:
    order = [t.priority for t in policy.tiers]
    if threshold not in order:
        raise InputError(f"--fail-on {threshold!r} is not a priority of the policy ({', '.join(order)})")
    return any(order.index(a.priority) <= order.index(threshold) for a in active)


def run_cvss(args: argparse.Namespace) -> int:
    vectors = list(args.vectors)
    if args.file:
        text = read_bytes(args.file).decode("utf-8")
        vectors += [line.split(" - ", 1)[0].strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    if not vectors:
        raise InputError("give at least one vector, or --file")
    results: List[Dict[str, Any]] = []
    for vector in vectors:
        try:
            s = score_vector(vector)
            results.append({"vector": s.vector, "version": s.version, "score": s.score, "severity": s.severity, "nomenclature": s.nomenclature, "macrovector": s.macrovector})
        except InputError as exc:
            results.append({"vector": vector, "score": None, "error": str(exc)})
    if args.json:
        print(json.dumps(results))
    else:
        for r in results:
            if r["score"] is None:
                print(f"error  {r['error']}")
                continue
            extra = f"  {r['nomenclature']} macrovector {r['macrovector']}" if r["macrovector"] else ""
            print(f"{r['score']:4}  {r['severity']:8}  {r['vector']}{extra}")
    return 1 if any(r["score"] is None for r in results) else 0


def run_vex(args: argparse.Namespace) -> int:
    rows, problems = [], []
    for path in args.files:
        statements, issues = load_vex(path)
        rows += statements
        problems += issues
    if args.json:
        print(json.dumps({"statements": [{"vulnerability": s.vulnerability, "aliases": list(s.aliases), "status": s.status, "justification": s.justification,
                                          "impact_statement": s.impact, "action_statement": s.action, "timestamp": s.timestamp, "author": s.author,
                                          "products": [{"id": p.id, "subcomponents": list(p.subcomponents)} for p in s.products]} for s in rows],
                          "problems": problems}, indent=2))
    else:
        for s in rows:
            print(f"{s.vulnerability:16}  {s.status:19}  {s.justification or s.action or s.impact}  products: {', '.join(p.id for p in s.products)}")
        for problem in problems:
            print(f"problem: {problem}")
    return 1 if problems else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "cvss":
            return run_cvss(args)
        if args.command == "vex":
            return run_vex(args)
        if args.command == "ingest":
            reports = [load_report(p) for p in args.reports]
            merged = merge(f for r in reports for f in r.findings)
            if args.json:
                print(json.dumps({"reports": [{"tool": r.tool, "path": r.path, "artifact": r.artifact, "created": r.created, "findings": len(r.findings)} for r in reports],
                                  "findings": [finding_json(f) for f in merged]}, indent=2))
            else:
                print(render_ingest(reports, merged))
            return 0
        if args.command == "rank":
            reports = [load_report(p) for p in args.reports]
            ctx = context_from(args, reports)
            active, suppressed = rank(merge(f for r in reports for f in r.findings), ctx)
            if args.json:
                print(json.dumps({"artifacts": sorted({r.artifact for r in reports}), "tools": sorted({r.tool for r in reports}), "exposure": ctx.exposure,
                                  "now": ctx.now.isoformat(), "policy": ctx.policy.name, "findings": [assessment_json(a) for a in active],
                                  "suppressed": [assessment_json(a) for a in suppressed], "summary": summary_of(active, suppressed, ctx.policy)}, indent=2))
            else:
                print(render_rank(reports, active, suppressed, ctx, args.top))
            return 1 if args.fail_on and priority_reached(active, args.fail_on, ctx.policy) else 0
        old_reports = [load_report(p) for p in args.old]
        new_reports = [load_report(p) for p in args.new]
        ctx = context_from(args, old_reports + new_reports)
        added, pairs, fixed = diff(merge(f for r in old_reports for f in r.findings), merge(f for r in new_reports for f in r.findings))
        for before, after in pairs:
            after.first_seen = before.first_seen or after.first_seen
        new_active, new_suppressed = rank(added, ctx)
        still_active, still_suppressed = rank([after for _, after in pairs], ctx)
        suppressed = sorted(new_suppressed + still_suppressed, key=lambda a: (a.finding.vuln, a.finding.package))
        if args.json:
            print(json.dumps({"old": [r.artifact for r in old_reports], "new": [r.artifact for r in new_reports], "now": ctx.now.isoformat(),
                              "warnings": [w for w in [tool_warning(old_reports, new_reports)] if w],
                              "new_findings": [assessment_json(a) for a in new_active], "still_open": [assessment_json(a) for a in still_active],
                              "fixed": [finding_json(f) for f in fixed], "suppressed": [assessment_json(a) for a in suppressed]}, indent=2))
        else:
            print(render_diff(old_reports, new_reports, new_active, still_active, fixed, suppressed))
        return 1 if new_active else 0
    except InputError as exc:
        print(f"minivuln: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
