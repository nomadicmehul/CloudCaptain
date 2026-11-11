#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

Runner = Callable[[Sequence[str]], Tuple[int, str, str]]

GIT_LOG_FORMAT = "%H%x1f%P%x1f%at%x1f%an%x1f%s%x1e"
HOUR = 3600
DAY = 24 * HOUR
TERMINAL_FAILURES = ("failure", "error")
VIEWS = ("daily", "weekly", "monthly")
LEVELS = ("elite", "high", "medium", "low")
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SCENARIO = os.path.join(HERE, "fixtures", "scenario.json")

CLUSTERS: Dict[str, Dict[str, Any]] = {
    "2023": {
        "report": "Accelerate State of DevOps Report 2023",
        "deploys_per_day_at_least": {"elite": 1.0, "high": 1 / 7, "medium": 1 / 30},
        "lead_time_under": {"elite": DAY, "high": 7 * DAY, "medium": 30 * DAY},
        "change_failure_rate": {"elite": 5.0, "high": 10.0, "medium": 15.0, "low": 64.0},
        "recovery_under": {"elite": HOUR, "high": DAY, "medium": 7 * DAY},
    },
    "2024": {
        "report": "Accelerate State of DevOps Report 2024",
        "deploys_per_day_at_least": {"elite": 1.0, "high": 1 / 7, "medium": 1 / 30},
        "lead_time_under": {"elite": DAY, "high": 7 * DAY, "medium": 30 * DAY},
        "change_failure_rate": {"elite": 5.0, "high": 20.0, "medium": 10.0, "low": 40.0},
        "recovery_under": {"elite": HOUR, "high": DAY, "medium": DAY},
    },
}


class InputError(Exception):
    pass


@dataclass
class Commit:
    sha: str
    parents: List[str]
    authored: int
    author: str
    subject: str


@dataclass
class Deploy:
    id: str
    sha: str
    time: int
    environment: str = "production"
    status: str = "success"
    rollback: bool = False


@dataclass
class Incident:
    id: str
    started: int
    resolved: Optional[int]
    root_cause: str = ""
    external: bool = False


@dataclass
class Change:
    head: str
    subject: str
    commits: List[str]
    first_authored: int
    deploy: str
    deployed: int
    commit_leads: List[int]

    @property
    def lead(self) -> int:
        return self.deployed - self.first_authored


@dataclass
class DeployResult:
    deploy: Deploy
    changes: List[Change] = field(default_factory=list)
    new_commits: Set[str] = field(default_factory=set)
    rollback_of: str = ""
    causes: List[str] = field(default_factory=list)
    restore_candidates: List[int] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.causes)

    @property
    def restored(self) -> Optional[int]:
        return min(self.restore_candidates) if self.failed and self.restore_candidates else None

    @property
    def recovery(self) -> Optional[int]:
        restored = self.restored
        return None if restored is None else restored - self.deploy.time


@dataclass
class IncidentLink:
    incident: Incident
    deploy: str
    how: str


@dataclass
class Period:
    since: int
    until: int

    @property
    def days(self) -> float:
        return (self.until - self.since) / DAY

    def contains(self, moment: int) -> bool:
        return self.since <= moment < self.until


@dataclass
class Report:
    period: Period
    environment: str
    window: int
    clusters: str
    deploys: List[DeployResult]
    changes: List[Change]
    links: List[IncidentLink]
    attempts: List[Deploy]
    metrics: Dict[str, Any]
    warnings: List[Dict[str, str]]
    problems: List[str]


def parse_time(text: Any, what: str) -> int:
    if not isinstance(text, str) or not text.strip():
        raise InputError(f"{what}: expected an ISO 8601 time such as 2026-09-07T10:00:00Z, got {text!r}")
    value = text.strip()
    if value.endswith("Z") or value.endswith("z"):
        value = value[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        raise InputError(f"{what}: {text!r} is not an ISO 8601 time")
    if stamp.tzinfo is None:
        raise InputError(f"{what}: {text!r} has no time zone; add Z or an offset such as +02:00")
    return int(stamp.timestamp())


def parse_day(text: str, what: str) -> int:
    try:
        day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise InputError(f"{what}: {text!r} is not a date in the form YYYY-MM-DD")
    return int(day.timestamp())


def iso(moment: int) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bucket(moment: int, view: str) -> str:
    stamp = datetime.fromtimestamp(moment, timezone.utc)
    if view == "daily":
        return stamp.strftime("%Y-%m-%d")
    if view == "weekly":
        year, week, _ = stamp.isocalendar()
        return f"{year}-W{week:02d}"
    return stamp.strftime("%Y-%m")


def floor_day(moment: int) -> int:
    return moment - moment % DAY


def percentile(values: Iterable[float], fraction: float) -> Optional[float]:
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def parse_git_log(text: str) -> Dict[str, Commit]:
    commits: Dict[str, Commit] = {}
    for record in text.split("\x1e"):
        record = record.strip("\n")
        if not record.strip():
            continue
        fields = record.split("\x1f")
        if len(fields) != 5:
            raise InputError(f"git log record has {len(fields)} fields, expected 5 (use --format={GIT_LOG_FORMAT}): {record[:60]!r}")
        sha, parents, authored, author, subject = fields
        try:
            stamp = int(authored)
        except ValueError:
            raise InputError(f"commit {sha[:12]}: author time {authored!r} is not a Unix timestamp")
        commits[sha] = Commit(sha, parents.split(), stamp, author, subject)
    return commits


def subprocess_runner(argv: Sequence[str]) -> Tuple[int, str, str]:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    except FileNotFoundError as error:
        return 127, "", str(error)
    return done.returncode, done.stdout, done.stderr


def load_commits(repo: str, runner: Runner = subprocess_runner) -> Dict[str, Commit]:
    code, out, err = runner(["git", "-C", repo, "log", "--all", f"--format={GIT_LOG_FORMAT}"])
    if code != 0:
        raise InputError(f"git log in {repo} failed (exit {code}): {err.strip()}")
    return parse_git_log(out)


def resolve(commits: Dict[str, Commit], sha: str) -> Optional[str]:
    if sha in commits:
        return sha
    if len(sha) < 7:
        return None
    matches = [full for full in commits if full.startswith(sha)]
    return matches[0] if len(matches) == 1 else None


def walk(commits: Dict[str, Commit], start: str, stop: Set[str]) -> Set[str]:
    seen: Set[str] = set()
    stack = [start]
    while stack:
        sha = stack.pop()
        if sha in seen or sha in stop or sha not in commits:
            continue
        seen.add(sha)
        stack.extend(commits[sha].parents)
    return seen


def is_proper_ancestor(commits: Dict[str, Commit], older: str, newer: str) -> bool:
    return older != newer and older in walk(commits, newer, set())


def split_changes(commits: Dict[str, Commit], tip: str, deployed: Set[str]) -> List[Tuple[str, List[str]]]:
    chain: List[str] = []
    current = tip
    while current in commits and current not in deployed:
        chain.append(current)
        parents = commits[current].parents
        current = parents[0] if parents else ""
    claimed = set(deployed)
    changes: List[Tuple[str, List[str]]] = []
    for head in reversed(chain):
        members = walk(commits, head, claimed)
        claimed |= members
        changes.append((head, sorted(members, key=lambda sha: (commits[sha].authored, sha))))
    return changes


def deploy_from_event(event: Dict[str, Any], line: str) -> Deploy:
    for key in ("id", "sha", "time"):
        if key not in event:
            raise InputError(f"{line}: deploy event has no {key!r}: {event}")
    status = str(event.get("status", "success")).lower()
    return Deploy(str(event["id"]), str(event["sha"]), parse_time(event["time"], f"{line} time"),
                  str(event.get("environment", "production")), status, bool(event.get("rollback", False)))


def deploy_from_github(item: Dict[str, Any], index: int) -> Deploy:
    where = f"deployment {item.get('id', index)}"
    for key in ("id", "sha", "statuses"):
        if key not in item:
            raise InputError(f"{where}: no {key!r}; attach each deployment's /statuses list under 'statuses'")
    statuses = sorted(item["statuses"], key=lambda status: parse_time(status.get("created_at"), f"{where} status"))
    success = [s for s in statuses if s.get("state") == "success"]
    failure = [s for s in statuses if s.get("state") in TERMINAL_FAILURES]
    if success:
        state, moment = "success", success[0]["created_at"]
    elif failure:
        state, moment = "failure", failure[0]["created_at"]
    else:
        state, moment = "pending", item.get("created_at") or (statuses[-1]["created_at"] if statuses else "")
    return Deploy(str(item["id"]), str(item["sha"]), parse_time(moment, f"{where} time"),
                  str(item.get("environment", "production")), state, False)


def load_deploys(path: str) -> List[Deploy]:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        raise InputError(f"cannot read deploys {path}: {error.strerror}")
    if text.lstrip().startswith("["):
        try:
            items = json.loads(text)
        except json.JSONDecodeError as error:
            raise InputError(f"{path}: not valid JSON: {error}")
        if all(isinstance(item, dict) and "statuses" in item for item in items):
            return [deploy_from_github(item, index) for index, item in enumerate(items)]
        return [deploy_from_event(item, f"{path} item {index}") for index, item in enumerate(items)]
    return [deploy_from_event(event, where) for where, event in json_lines(path, text)]


def json_lines(path: str, text: str) -> List[Tuple[str, Dict[str, Any]]]:
    events = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise InputError(f"{path}:{number}: not valid JSON: {error}")
        if not isinstance(event, dict):
            raise InputError(f"{path}:{number}: expected a JSON object")
        events.append((f"{path}:{number}", event))
    return events


def load_incidents(path: str) -> List[Incident]:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        raise InputError(f"cannot read incidents {path}: {error.strerror}")
    incidents = []
    for where, event in json_lines(path, text):
        if "id" not in event or "started" not in event:
            raise InputError(f"{where}: an incident needs 'id' and 'started'")
        resolved = event.get("resolved")
        incidents.append(Incident(str(event["id"]), parse_time(event["started"], f"{where} started"),
                                  None if resolved in (None, "") else parse_time(resolved, f"{where} resolved"),
                                  str(event.get("root_cause") or ""), bool(event.get("external", False))))
    return incidents


def attribute(commits: Dict[str, Commit], deploys: List[Deploy], problems: List[str]) -> List[DeployResult]:
    results: List[DeployResult] = []
    deployed: Set[str] = set()
    for deploy in sorted(deploys, key=lambda d: (d.time, d.id)):
        full = resolve(commits, deploy.sha)
        if full is None:
            problems.append(f"deploy {deploy.id}: commit {deploy.sha} is not in the Git history; it was left out")
            continue
        deploy.sha = full
        result = DeployResult(deploy)
        if results:
            previous = results[-1].deploy
            if deploy.rollback or is_proper_ancestor(commits, full, previous.sha):
                result.rollback_of = previous.id
        for head, members in split_changes(commits, full, deployed):
            first = min(commits[sha].authored for sha in members)
            leads = [deploy.time - commits[sha].authored for sha in members]
            result.changes.append(Change(head, commits[head].subject, members, first, deploy.id, deploy.time, leads))
            result.new_commits.update(members)
        deployed |= result.new_commits
        results.append(result)
    return results


def link_incidents(results: List[DeployResult], incidents: List[Incident], window: int,
                   commits: Dict[str, Commit], problems: List[str]) -> List[IncidentLink]:
    by_id = {result.deploy.id: result for result in results}
    rollback_times: Dict[str, List[int]] = {}
    for result in results:
        if result.rollback_of and result.deploy.time - by_id[result.rollback_of].deploy.time < window:
            by_id[result.rollback_of].causes.append(f"rolled back by {result.deploy.id}")
            rollback_times.setdefault(result.rollback_of, []).append(result.deploy.time)
    links = []
    for incident in sorted(incidents, key=lambda i: (i.started, i.id)):
        if incident.resolved is not None and incident.resolved < incident.started:
            problems.append(f"incident {incident.id}: resolved before it started; it was left out")
            continue
        target: Optional[DeployResult] = None
        how = "unlinked"
        if incident.external:
            how = "external"
        elif incident.root_cause:
            full = resolve(commits, incident.root_cause)
            target = next((r for r in results if full is not None and full in r.new_commits), None)
            if target is None:
                problems.append(f"incident {incident.id}: root cause {incident.root_cause} was never deployed")
            else:
                how = "root cause"
        else:
            before = [r for r in results if r.deploy.time <= incident.started and incident.started - r.deploy.time < window]
            if before:
                target, how = before[-1], "window"
        if target is not None:
            target.causes.append(f"incident {incident.id}")
        links.append(IncidentLink(incident, target.deploy.id if target else "", how))
    for result in results:
        linked = [link.incident for link in links if link.deploy == result.deploy.id]
        candidates = list(rollback_times.get(result.deploy.id, []))
        if linked and all(incident.resolved is not None for incident in linked):
            candidates.append(max(incident.resolved for incident in linked if incident.resolved is not None))
        result.restore_candidates = candidates
    return links


def level_at_least(value: float, thresholds: Dict[str, float]) -> str:
    for level in LEVELS[:-1]:
        if level in thresholds and value >= thresholds[level]:
            return level
    return "low"


def level_under(value: float, thresholds: Dict[str, float]) -> str:
    for level in LEVELS[:-1]:
        if level in thresholds and value < thresholds[level]:
            return level
    return "low"


def level_nearest(value: float, points: Dict[str, float]) -> str:
    return min(LEVELS, key=lambda level: (abs(points[level] - value), -LEVELS.index(level)))


def default_period(deploys: List[Deploy]) -> Period:
    if not deploys:
        raise InputError("no successful deploys to the environment; nothing to measure")
    return Period(floor_day(min(d.time for d in deploys)), floor_day(max(d.time for d in deploys)) + DAY)


def days_in(period: Period) -> List[int]:
    return list(range(floor_day(period.since), period.until, DAY))


def deploy_days_per_week(results: List[DeployResult], period: Period) -> float:
    weeks: Dict[str, Set[str]] = {}
    for day in days_in(period):
        weeks.setdefault(bucket(day, "weekly"), set())
    for result in results:
        weeks.setdefault(bucket(result.deploy.time, "weekly"), set()).add(bucket(result.deploy.time, "daily"))
    return float(statistics.median(len(days) for days in weeks.values())) if weeks else 0.0


def summarize(results: List[DeployResult], links: List[IncidentLink], period: Period, clusters: str) -> Dict[str, Any]:
    table = CLUSTERS[clusters]
    count = len(results)
    per_day = count / period.days if period.days > 0 else 0.0
    changes = [change for result in results for change in result.changes]
    leads = [change.lead for change in changes]
    commit_leads = [lead for change in changes for lead in change.commit_leads if lead > 0]
    failed = [result for result in results if result.failed]
    recoveries = [result.recovery for result in failed if result.recovery is not None]
    restores = [link.incident.resolved - link.incident.started for link in links
                if link.incident.resolved is not None and period.contains(link.incident.started)]
    rate = 100.0 * len(failed) / count if count else None
    lead_median = percentile(leads, 0.5)
    recovery_median = percentile(recoveries, 0.5)
    return {
        "deployment_frequency": {
            "deploys": count,
            "per_day": per_day,
            "per_week": per_day * 7,
            "median_deploy_days_per_week": deploy_days_per_week(results, period),
            "level": level_at_least(per_day, table["deploys_per_day_at_least"]) if count else None,
        },
        "lead_time": {
            "changes": len(leads),
            "median_seconds": lead_median,
            "p90_seconds": percentile(leads, 0.9),
            "per_commit_median_seconds": percentile(commit_leads, 0.5),
            "commits": len(commit_leads),
            "level": level_under(lead_median, table["lead_time_under"]) if lead_median is not None else None,
        },
        "change_failure_rate": {
            "deploys": count,
            "failed": len(failed),
            "percent": rate,
            "level": level_nearest(rate, table["change_failure_rate"]) if rate is not None else None,
        },
        "failed_deployment_recovery_time": {
            "failed": len(failed),
            "restored": len(recoveries),
            "median_seconds": recovery_median,
            "p90_seconds": percentile(recoveries, 0.9),
            "level": level_under(recovery_median, table["recovery_under"]) if recovery_median is not None else None,
        },
        "incident_time_to_restore": {
            "incidents": len(restores),
            "median_seconds": percentile(restores, 0.5),
        },
    }


def warnings_for(results: List[DeployResult], links: List[IncidentLink], metrics: Dict[str, Any]) -> List[Dict[str, str]]:
    found: List[Dict[str, str]] = []

    def warn(key: str, message: str) -> None:
        found.append({"id": key, "message": message})

    deploys = metrics["deployment_frequency"]["deploys"]
    changes = [change for result in results for change in result.changes]
    if deploys < 10 or len(changes) < 10:
        warn("small-sample", f"{deploys} deploys and {len(changes)} changes: a median or p90 over so few values moves with a single event; compare periods, not single numbers")
    empty = [r.deploy.id for r in results if not r.changes and not r.rollback_of]
    if empty:
        warn("empty-deploys", f"{len(empty)} deploy(s) shipped no new commit ({', '.join(empty)}): redeploying the same commit raises deployment frequency without delivering a change")
    frequency = metrics["deployment_frequency"]
    if frequency["per_day"] >= 1 and frequency["median_deploy_days_per_week"] < 3:
        warn("bursty-deploys", f"{frequency['per_day']:.2f} deploys per day but a median of {frequency['median_deploy_days_per_week']:g} deploy days per week: the frequency comes from bursts, not from shipping every day")
    if deploys >= 10 and metrics["change_failure_rate"]["failed"] == 0:
        warn("no-failures-recorded", f"0 of {deploys} deploys failed: a 0% change failure rate usually means incidents and rollbacks are not linked to deploys, not that nothing broke")
    unlinked = [link.incident.id for link in links if link.how == "unlinked"]
    if unlinked:
        warn("unlinked-incidents", f"incident(s) {', '.join(unlinked)} could not be tied to a deploy (no root cause commit, no deploy within the window): they count in neither the change failure rate nor the recovery time")
    open_incidents = [link.incident.id for link in links if link.incident.resolved is None]
    if open_incidents:
        warn("open-incidents", f"incident(s) {', '.join(open_incidents)} are not resolved: their recovery time is missing, so the median looks better than it is")
    single = sum(1 for change in changes if len(change.commits) == 1)
    if len(changes) >= 5 and single >= 0.8 * len(changes):
        warn("single-commit-changes", f"{single} of {len(changes)} changes are a single commit: with squash or rebase merges the clock starts at the squashed commit, so lead time leaves out the time the work spent in review")
    return found


def compute(commits: Dict[str, Commit], deploys: List[Deploy], incidents: List[Incident], environment: str = "production",
            window_hours: float = 24.0, since: Optional[int] = None, until: Optional[int] = None,
            clusters: str = "2024") -> Report:
    if clusters not in CLUSTERS:
        raise InputError(f"unknown cluster year {clusters!r}; known: {', '.join(sorted(CLUSTERS))}")
    problems: List[str] = []
    window = int(window_hours * HOUR)
    in_environment = [d for d in deploys if d.environment == environment]
    successful = [d for d in in_environment if d.status == "success"]
    attempts = [d for d in in_environment if d.status != "success"]
    results = attribute(commits, successful, problems)
    links = link_incidents(results, incidents, window, commits, problems)
    period = default_period([r.deploy for r in results]) if since is None or until is None else Period(since, until)
    if since is not None and until is None:
        period = Period(since, period.until)
    if until is not None and since is None:
        period = Period(period.since, until)
    if period.until <= period.since:
        raise InputError(f"the period {iso(period.since)} to {iso(period.until)} is empty")
    inside = [r for r in results if period.contains(r.deploy.time)]
    metrics = summarize(inside, links, period, clusters)
    warnings = warnings_for(inside, links, metrics)
    changes = [change for result in inside for change in result.changes]
    return Report(period, environment, window, clusters, inside, changes, links, attempts, metrics, warnings, problems)


def view_rows(report: Report, view: str) -> List[Dict[str, Any]]:
    keys: List[str] = []
    for day in days_in(report.period):
        key = bucket(day, view)
        if key not in keys:
            keys.append(key)
    rows = []
    for key in keys:
        results = [r for r in report.deploys if bucket(r.deploy.time, view) == key]
        leads = [change.lead for r in results for change in r.changes]
        failed = [r for r in results if r.failed]
        recoveries = [r.recovery for r in failed if r.recovery is not None]
        rows.append({
            "bucket": key,
            "deploys": len(results),
            "changes": len(leads),
            "lead_median_seconds": percentile(leads, 0.5),
            "failed": len(failed),
            "change_failure_percent": 100.0 * len(failed) / len(results) if results else None,
            "recovery_median_seconds": percentile(recoveries, 0.5),
        })
    return rows


def report_json(report: Report, view: str) -> Dict[str, Any]:
    return {
        "period": {"since": iso(report.period.since), "until": iso(report.period.until), "days": report.period.days},
        "environment": report.environment,
        "window_hours": report.window / HOUR,
        "clusters": {"year": report.clusters, "report": CLUSTERS[report.clusters]["report"]},
        "metrics": report.metrics,
        "deploys": [{
            "id": r.deploy.id, "sha": r.deploy.sha, "time": iso(r.deploy.time),
            "changes": [c.head for c in r.changes], "new_commits": len(r.new_commits),
            "rollback_of": r.rollback_of, "failed": r.failed, "causes": r.causes,
            "restored": iso(r.restored) if r.restored is not None else None, "recovery_seconds": r.recovery,
        } for r in report.deploys],
        "changes": [{
            "head": c.head, "subject": c.subject, "commits": c.commits, "first_commit": iso(c.first_authored),
            "deploy": c.deploy, "deployed": iso(c.deployed), "lead_seconds": c.lead,
        } for c in report.changes],
        "incidents": [{
            "id": link.incident.id, "started": iso(link.incident.started),
            "resolved": iso(link.incident.resolved) if link.incident.resolved is not None else None,
            "deploy": link.deploy, "how": link.how,
        } for link in report.links],
        "attempts": [{"id": d.id, "sha": d.sha, "time": iso(d.time), "status": d.status} for d in report.attempts],
        "view": {"name": view, "rows": view_rows(report, view)},
        "warnings": report.warnings,
        "problems": report.problems,
    }


def hours(seconds: Optional[float]) -> str:
    return "-" if seconds is None else f"{seconds / HOUR:.1f} h"


def render_text(report: Report, view: str, show_changes: bool) -> str:
    m = report.metrics
    df, lt, cfr, rec, mttr = (m["deployment_frequency"], m["lead_time"], m["change_failure_rate"],
                              m["failed_deployment_recovery_time"], m["incident_time_to_restore"])
    percent = "-" if cfr["percent"] is None else f"{cfr['percent']:.2f}%"
    lines = [
        f"minidora: {report.environment}, {iso(report.period.since)[:10]} to {iso(report.period.until)[:10]} ({report.period.days:g} days), "
        f"incidents linked within {report.window / HOUR:g} h, levels per metric from the {CLUSTERS[report.clusters]['report']}",
        f"  deployment frequency       {df['deploys']} deploys, {df['per_day']:.2f} per day, median {df['median_deploy_days_per_week']:g} deploy days per week  [{df['level']}]",
        f"  lead time for changes      {lt['changes']} changes, median {hours(lt['median_seconds'])}, p90 {hours(lt['p90_seconds'])}; per commit (Four Keys) median {hours(lt['per_commit_median_seconds'])}  [{lt['level']}]",
        f"  change failure rate        {cfr['failed']} of {cfr['deploys']} deploys = {percent}  [{cfr['level']}]",
        f"  failed deployment recovery {rec['restored']} restored, median {hours(rec['median_seconds'])}, p90 {hours(rec['p90_seconds'])}  [{rec['level']}]",
        f"  incident time to restore   {mttr['incidents']} incidents, median {hours(mttr['median_seconds'])} (the pre-2023 MTTR, every incident)",
        f"{view} view",
        f"  {'bucket':<11} {'deploys':>7} {'changes':>7} {'lead p50':>9} {'failed':>6} {'cfr':>7} {'recovery p50':>12}",
    ]
    for row in view_rows(report, view):
        rate = "-" if row["change_failure_percent"] is None else f"{row['change_failure_percent']:.1f}%"
        lines.append(f"  {row['bucket']:<11} {row['deploys']:>7} {row['changes']:>7} {hours(row['lead_median_seconds']):>9} "
                     f"{row['failed']:>6} {rate:>7} {hours(row['recovery_median_seconds']):>12}")
    failed = [r for r in report.deploys if r.failed]
    lines.append("failed deploys" + ("" if failed else ": none"))
    for r in failed:
        lines.append(f"  {r.deploy.id:<8} {iso(r.deploy.time)}  {', '.join(r.causes)}; recovered after {hours(r.recovery)}")
    for link in report.links:
        if link.how in ("external", "unlinked"):
            lines.append(f"  incident {link.incident.id} is {link.how}: not a change failure")
    if show_changes:
        lines.append("changes")
        for change in report.changes:
            lines.append(f"  {change.head[:10]}  {len(change.commits)} commit(s)  first {iso(change.first_authored)}  {change.deploy:<8} lead {hours(change.lead):>8}  {change.subject}")
    for item in report.warnings:
        lines.append(f"warning [{item['id']}] {item['message']}")
    for problem in report.problems:
        lines.append(f"problem: {problem}")
    return "\n".join(lines) + "\n"


def git_env(when: int, author: str) -> Dict[str, str]:
    stamp = iso(when).replace("Z", "+00:00")
    email = f"{author}@example.com"
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_NAME": author,
                "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": email,
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    return env


def git_at(repo: str, when: int, author: str, *args: str) -> str:
    argv = ["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", "-C", repo, *args]
    done = subprocess.run(argv, capture_output=True, text=True, env=git_env(when, author), check=False)
    if done.returncode != 0:
        raise InputError(f"{' '.join(argv[5:])} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def scenario_events(scenario: Dict[str, Any], start: int) -> List[Tuple[int, int, str, Dict[str, Any], int]]:
    events = []
    for change in scenario["changes"]:
        for index, hour in enumerate(change["commits"]):
            events.append((start + int(round(hour * HOUR)), 0, "direct" if change.get("direct") else "commit", change, index))
        if not change.get("direct"):
            if "merged" not in change:
                raise InputError(f"change {change['id']}: a pull request needs 'merged' (or set 'direct': true)")
            events.append((start + int(round(change["merged"] * HOUR)), 1, "merge", change, 0))
    return sorted(events, key=lambda e: (e[0], e[1], e[3]["id"], e[4]))


def build_demo(scenario: Dict[str, Any], target: str) -> Dict[str, str]:
    start = parse_time(scenario["start"], "scenario start")
    repo = os.path.join(target, "repo")
    os.makedirs(os.path.join(repo, "changes"), exist_ok=True)
    git_at(repo, start, "minidora", "init", "-q")
    heads: Dict[str, str] = {}
    tips: Dict[str, str] = {}
    branches: Set[str] = set()
    on_main = False
    for when, _, kind, change, index in scenario_events(scenario, start):
        name, author = change["id"], change.get("author", "dev")
        if kind != "direct" and not on_main:
            raise InputError(f"change {name}: the first change must be direct, so that main exists to branch from")
        if kind == "merge":
            git_at(repo, when, author, "checkout", "-q", "main")
            git_at(repo, when, author, "merge", "-q", "--no-ff", "-m", f"Merge {name}: {change['title']}", name)
            heads[name] = git_at(repo, when, author, "rev-parse", "HEAD")
            continue
        if kind == "commit":
            git_at(repo, when, author, "checkout", "-q", *([name] if name in branches else ["-b", name, "main"]))
            branches.add(name)
        elif on_main:
            git_at(repo, when, author, "checkout", "-q", "main")
        with open(os.path.join(repo, "changes", f"{name}.txt"), "a", encoding="utf-8") as handle:
            handle.write(f"{change['title']} ({index + 1}/{len(change['commits'])})\n")
        git_at(repo, when, author, "add", "-A")
        suffix = "" if len(change["commits"]) == 1 else f" ({index + 1}/{len(change['commits'])})"
        git_at(repo, when, author, "commit", "-q", "-m", f"{change['title']}{suffix}")
        tips[name] = git_at(repo, when, author, "rev-parse", "HEAD")
        if kind == "direct":
            heads[name] = tips[name]
            on_main = True
    return write_demo_logs(scenario, target, start, heads, tips)


def write_demo_logs(scenario: Dict[str, Any], target: str, start: int, heads: Dict[str, str], tips: Dict[str, str]) -> Dict[str, str]:
    paths = {name: os.path.join(target, name) for name in ("deploys.jsonl", "incidents.jsonl", "github-deployments.json", "refs.json")}
    events, github = [], []
    latest_success: Dict[str, Dict[str, Any]] = {}
    for number, deploy in enumerate(scenario["deploys"], 1):
        if deploy["ref"] not in heads:
            raise InputError(f"deploy {deploy['id']}: unknown change {deploy['ref']!r}")
        when = start + int(round(deploy["at"] * HOUR))
        environment, status = deploy.get("environment", "production"), deploy.get("status", "success")
        events.append({"id": deploy["id"], "sha": heads[deploy["ref"]], "environment": environment, "status": status, "time": iso(when)})
        item = {"id": 9000 + number, "sha": heads[deploy["ref"]], "ref": "main", "task": "deploy", "environment": environment,
                "description": deploy["id"], "production_environment": environment == "production", "created_at": iso(when - 300),
                "statuses": [{"state": status, "created_at": iso(when)}, {"state": "in_progress", "created_at": iso(when - 240)}]}
        if status == "success":
            if environment in latest_success:
                latest_success[environment]["statuses"].insert(0, {"state": "inactive", "created_at": iso(when)})
            latest_success[environment] = item
        github.append(item)
    incidents = []
    for incident in scenario["incidents"]:
        record: Dict[str, Any] = {"id": incident["id"], "title": incident.get("title", ""),
                                  "started": iso(start + int(round(incident["started"] * HOUR)))}
        if incident.get("resolved") is not None:
            record["resolved"] = iso(start + int(round(incident["resolved"] * HOUR)))
        if incident.get("root_cause"):
            record["root_cause"] = tips[incident["root_cause"]]
        if incident.get("external"):
            record["external"] = True
        incidents.append(record)
    with open(paths["deploys.jsonl"], "w", encoding="utf-8") as handle:
        handle.writelines(json.dumps(event) + "\n" for event in events)
    with open(paths["incidents.jsonl"], "w", encoding="utf-8") as handle:
        handle.writelines(json.dumps(record) + "\n" for record in incidents)
    with open(paths["github-deployments.json"], "w", encoding="utf-8") as handle:
        json.dump(list(reversed(github)), handle, indent=2)
    with open(paths["refs.json"], "w", encoding="utf-8") as handle:
        json.dump({"heads": heads, "tips": tips}, handle, indent=2, sort_keys=True)
    paths["repo"] = os.path.join(target, "repo")
    return paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minidora", description="The DORA software delivery metrics from Git history, a deploy log and an incident log.")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="build a Git repository with a scripted history plus deploy and incident logs")
    demo.add_argument("target", help="directory to create (must not exist or be empty)")
    demo.add_argument("--scenario", default=DEFAULT_SCENARIO, help="scenario JSON (default: code/fixtures/scenario.json)")
    report = sub.add_parser("report", help="compute the metrics")
    source = report.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", help="Git repository to read with git log --all")
    source.add_argument("--git-log", help=f"file with the output of git log --all --format='{GIT_LOG_FORMAT}'")
    report.add_argument("--deploys", required=True, help="deploy events as JSON Lines, or a JSON array of GitHub deployments with their statuses")
    report.add_argument("--incidents", help="incidents as JSON Lines: id, started, resolved, optional root_cause commit and external")
    report.add_argument("--environment", default="production")
    report.add_argument("--window-hours", type=float, default=24.0, help="an incident or rollback this soon after a deploy fails it (default 24)")
    report.add_argument("--since", help="first day of the period, YYYY-MM-DD (UTC)")
    report.add_argument("--until", help="day after the period, YYYY-MM-DD (UTC, exclusive)")
    report.add_argument("--view", choices=VIEWS, default="weekly")
    report.add_argument("--clusters", choices=sorted(CLUSTERS), default="2024", help="which State of DevOps report's performance levels to compare with")
    report.add_argument("--changes", action="store_true", help="list every change with its lead time")
    report.add_argument("--json", action="store_true")
    return parser


def run_report(args: argparse.Namespace, runner: Runner) -> int:
    if args.window_hours <= 0:
        raise InputError("--window-hours must be positive")
    if args.git_log:
        try:
            with open(args.git_log, encoding="utf-8") as handle:
                commits = parse_git_log(handle.read())
        except OSError as error:
            raise InputError(f"cannot read {args.git_log}: {error.strerror}")
    else:
        commits = load_commits(args.repo, runner)
    deploys = load_deploys(args.deploys)
    incidents = load_incidents(args.incidents) if args.incidents else []
    since = parse_day(args.since, "--since") if args.since else None
    until = parse_day(args.until, "--until") if args.until else None
    report = compute(commits, deploys, incidents, args.environment, args.window_hours, since, until, args.clusters)
    if args.json:
        print(json.dumps(report_json(report, args.view), indent=2))
    else:
        sys.stdout.write(render_text(report, args.view, args.changes))
    for problem in report.problems:
        print(f"minidora: {problem}", file=sys.stderr)
    return 1 if report.problems else 0


def run_demo(args: argparse.Namespace) -> int:
    if os.path.exists(args.target) and os.listdir(args.target):
        raise InputError(f"{args.target} exists and is not empty")
    try:
        with open(args.scenario, encoding="utf-8") as handle:
            scenario = json.load(handle)
    except OSError as error:
        raise InputError(f"cannot read {args.scenario}: {error.strerror}")
    except json.JSONDecodeError as error:
        raise InputError(f"{args.scenario}: not valid JSON: {error}")
    paths = build_demo(scenario, args.target)
    for name in ("repo", "deploys.jsonl", "incidents.jsonl", "github-deployments.json", "refs.json"):
        print(f"{name:<24} {paths[name]}")
    return 0


def main(argv: Optional[Sequence[str]] = None, runner: Runner = subprocess_runner) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exit_:
        return int(exit_.code or 0)
    try:
        return run_demo(args) if args.command == "demo" else run_report(args, runner)
    except InputError as error:
        print(f"minidora: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
