"""driftwatch: run a refresh-only plan, classify what changed outside OpenTofu, exit like -detailed-exitcode, write a Markdown report."""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

EXIT_CLEAN = 0
EXIT_ERROR = 1
EXIT_DRIFT = 2

CHANGED = "changed-outside"
DELETED = "deleted-outside"
REPLACE = "replace-on-apply"
IGNORED = "ignored"
CATEGORY_ORDER = [REPLACE, DELETED, CHANGED, IGNORED]
LABELS = {
    CHANGED: "changed outside",
    DELETED: "deleted outside",
    REPLACE: "replaced by the next apply",
    IGNORED: "ignored",
}
SYMBOLS = {CHANGED: "~", DELETED: "-", REPLACE: "-/+", IGNORED: "="}
PENDING_SYMBOLS = {"create": "+", "update": "~", "replace": "-/+", "delete": "-"}
NEXT_WORDS = {
    "update": "updates it in place, reverting the change",
    "create": "creates it again",
    "replace": "destroys it and creates a new one",
    "delete": "deletes it",
    "no-op": "leaves it alone",
}

Segment = Union[str, int]
Path = Tuple[Segment, ...]
Rule = Tuple[str, Path]
Runner = Callable[[Sequence[str]], Tuple[int, str, str]]

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
RULE_TOKEN = re.compile(r'\s*(?:\.?([A-Za-z_][A-Za-z0-9_-]*)|\.(\d+)|\[\s*(\d+)\s*\]|\[\s*"((?:[^"\\]|\\.)*)"\s*\])')


class DriftError(Exception):
    pass


@dataclass
class AttrDiff:
    path: str
    before: Any
    after: Any
    sensitive: bool = False


@dataclass
class Finding:
    address: str
    type: str
    provider: str
    category: str
    drift: str
    diffs: List[AttrDiff] = field(default_factory=list)
    ignored_by: List[str] = field(default_factory=list)
    next_apply: Optional[str] = None
    replace_paths: List[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class Pending:
    address: str
    action: str
    reason: str = ""


@dataclass
class Report:
    stack: str
    findings: List[Finding]
    pending: List[Pending]
    full_plan: bool
    tofu_exit: Optional[int] = None
    timestamp: str = ""


def subprocess_runner(argv: Sequence[str]) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: command not found (install OpenTofu or pass --tofu)"
    return proc.returncode, proc.stdout, proc.stderr


def tail(text: str, lines: int = 6) -> str:
    kept = [line for line in text.strip().splitlines() if line.strip()]
    return " | ".join(kept[-lines:]) or "(no output)"


def plan_argv(binary: str, chdir: str, out: str, refresh_only: bool, lock_timeout: str) -> List[str]:
    argv = [binary, f"-chdir={chdir}", "plan"]
    if refresh_only:
        argv.append("-refresh-only")
    argv += ["-input=false", "-no-color", "-detailed-exitcode", f"-lock-timeout={lock_timeout}", f"-out={out}"]
    return argv


def run_plan(runner: Runner, binary: str, chdir: str, out: str, refresh_only: bool, lock_timeout: str) -> Tuple[int, Dict[str, Any]]:
    label = "refresh-only plan" if refresh_only else "plan"
    code, _, err = runner(plan_argv(binary, chdir, out, refresh_only, lock_timeout))
    if code not in (EXIT_CLEAN, EXIT_DRIFT):
        raise DriftError(f"{label} failed (exit {code}): {tail(err)}")
    show_code, stdout, show_err = runner([binary, f"-chdir={chdir}", "show", "-json", out])
    if show_code != 0:
        raise DriftError(f"show -json of the {label} failed (exit {show_code}): {tail(show_err)}")
    return code, parse_plan(stdout, label)


def parse_plan(text: str, source: str) -> Dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DriftError(f"{source}: not JSON ({exc.msg} at line {exc.lineno}); pass the output of `tofu show -json PLANFILE`")
    if not isinstance(data, dict) or "format_version" not in data:
        raise DriftError(f"{source}: no format_version; this is not the output of `tofu show -json PLANFILE`")
    if not str(data["format_version"]).startswith("1."):
        raise DriftError(f"{source}: unsupported format_version {data['format_version']}; driftwatch reads 1.x")
    if "values" in data and "planned_values" not in data:
        raise DriftError(f"{source}: this is state JSON; save a plan with -out and run `tofu show -json` on the plan file")
    if data.get("errored"):
        raise DriftError(f"{source}: the plan errored, so its drift list is incomplete; fix the errors and plan again")
    return data


def flatten(value: Any, prefix: Path = ()) -> Dict[Path, Any]:
    if isinstance(value, dict):
        flat: Dict[Path, Any] = {}
        for key in sorted(value):
            flat.update(flatten(value[key], prefix + (key,)))
        return flat
    if isinstance(value, list):
        flat = {}
        for index, item in enumerate(value):
            flat.update(flatten(item, prefix + (index,)))
        return flat
    if value is None:
        return {}
    return {prefix: value}


def path_key(path: Path) -> Tuple[Tuple[int, int, str], ...]:
    return tuple((0, segment, "") if isinstance(segment, int) else (1, 0, segment) for segment in path)


def render_path(path: Sequence[Segment]) -> str:
    text = ""
    for segment in path:
        if isinstance(segment, int):
            text += f"[{segment}]"
        elif IDENTIFIER.match(segment):
            text += f".{segment}" if text else segment
        else:
            text += f"[{json.dumps(segment)}]"
    return text


def is_sensitive(marks: Any, path: Path) -> bool:
    for segment in path:
        if marks is True:
            return True
        if isinstance(marks, dict) and isinstance(segment, str):
            marks = marks.get(segment)
        elif isinstance(marks, list) and isinstance(segment, int) and segment < len(marks):
            marks = marks[segment]
        else:
            return False
    return marks is True


def attribute_changes(change: Mapping[str, Any]) -> List[Tuple[Path, AttrDiff]]:
    before = flatten(change.get("before"))
    after = flatten(change.get("after"))
    pairs: List[Tuple[Path, AttrDiff]] = []
    for path in sorted(set(before) | set(after), key=path_key):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        secret = is_sensitive(change.get("before_sensitive"), path) or is_sensitive(change.get("after_sensitive"), path)
        pairs.append((path, AttrDiff(render_path(path), None if secret else old, None if secret else new, secret)))
    return pairs


def parse_rule(text: str) -> Path:
    source = text.strip()
    if source == "all":
        return ()
    segments: List[Segment] = []
    position = 0
    while position < len(source):
        match = RULE_TOKEN.match(source, position)
        if not match or match.end() == position:
            raise DriftError(f"cannot parse ignore_changes entry {text!r} at character {position + 1}")
        name, dotted_index, index, key = match.groups()
        if name is not None:
            segments.append(name)
        elif dotted_index is not None:
            segments.append(int(dotted_index))
        elif index is not None:
            segments.append(int(index))
        else:
            segments.append(json.loads(f'"{key}"'))
        position = match.end()
    if not segments:
        raise DriftError(f"empty ignore_changes entry {text!r}")
    return tuple(segments)


def covers(rule: Path, path: Path) -> bool:
    return path[: len(rule)] == rule


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def load_ignore_rules(paths: Sequence[str]) -> Dict[str, List[Rule]]:
    rules: Dict[str, List[Rule]] = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise DriftError(f"{path}: not JSON ({exc.msg} at line {exc.lineno}); driftwatch reads OpenTofu's .tf.json syntax")
        for by_type in as_list(data.get("resource")):
            for rtype, by_name_list in by_type.items():
                for by_name in as_list(by_name_list):
                    for name, bodies in by_name.items():
                        for body in as_list(bodies):
                            for lifecycle in as_list(body.get("lifecycle")):
                                entries = lifecycle.get("ignore_changes")
                                if entries is None:
                                    continue
                                for entry in as_list(entries):
                                    rules.setdefault(f"{rtype}.{name}", []).append((entry, parse_rule(entry)))
    return rules


def config_address(address: str) -> str:
    return re.sub(r'\[(?:"(?:[^"\\]|\\.)*"|[^\]]*)\]', "", address)


def action_name(actions: Sequence[str]) -> str:
    actions = list(actions)
    if "delete" in actions and "create" in actions:
        return "replace"
    if not actions or actions == ["no-op"]:
        return "no-op"
    return "+".join(actions)


def short_provider(name: str) -> str:
    return "/".join(name.split("/")[-2:]) if name else ""


def explain(finding: Finding) -> str:
    if finding.next_apply is None:
        consequence = "run a normal plan as well (drop --drift-only) to see what the next apply does"
    else:
        consequence = f"the next apply {NEXT_WORDS.get(finding.next_apply, finding.next_apply)}"
    count = f"{len(finding.diffs)} attribute(s) changed"
    if finding.category == DELETED:
        return f"the provider no longer finds the object recorded in state; {consequence}"
    if finding.category == REPLACE:
        forced = ", ".join(finding.replace_paths) or "an attribute that cannot change in place"
        return f"{count}; {forced} forces replacement, so {consequence}"
    if finding.category == IGNORED:
        if finding.next_apply not in (None, "no-op"):
            consequence = f"the next apply still does {finding.next_apply} for a configuration change, not to revert this"
        if finding.ignored_by:
            return f"{count}, covered by ignore_changes ({', '.join(finding.ignored_by)}); {consequence}"
        return f"{count}, none of them set by the configuration; {consequence}"
    return f"{count}; {consequence}"


def classify(refresh: Mapping[str, Any], plan: Optional[Mapping[str, Any]] = None, rules: Optional[Mapping[str, List[Rule]]] = None, stack: str = "default") -> Report:
    rules = rules or {}
    planned: Dict[str, Mapping[str, Any]] = {}
    if plan is not None:
        for change in plan.get("resource_changes") or []:
            if change.get("mode", "managed") == "managed" and not change.get("deposed"):
                planned[change["address"]] = change
    findings: List[Finding] = []
    for drift in refresh.get("resource_drift") or []:
        if drift.get("mode", "managed") != "managed":
            continue
        address = drift["address"]
        change = drift.get("change") or {}
        actions = change.get("actions") or []
        next_change = planned.get(address)
        finding = Finding(
            address=address,
            type=drift.get("type", ""),
            provider=short_provider(drift.get("provider_name", "")),
            category=CHANGED,
            drift="delete" if "delete" in actions else "update",
            next_apply=action_name(next_change["change"].get("actions") or []) if next_change else None,
        )
        if finding.drift == "delete":
            finding.category = DELETED
        else:
            pairs = attribute_changes(change)
            finding.diffs = [diff for _, diff in pairs]
            matched = rules.get(config_address(address), [])
            covered = [any(covers(rule, path) for _, rule in matched) for path, _ in pairs]
            finding.ignored_by = sorted({text for text, rule in matched if any(covers(rule, path) for path, _ in pairs)})
            if finding.next_apply == "replace":
                finding.category = REPLACE
                finding.replace_paths = [render_path(path) for path in next_change["change"].get("replace_paths") or []]
            elif (pairs and all(covered)) or finding.next_apply == "no-op":
                finding.category = IGNORED
        finding.reason = explain(finding)
        findings.append(finding)
    drifted = {finding.address for finding in findings}
    pending = []
    for address, change in planned.items():
        name = action_name(change["change"].get("actions") or [])
        if name in ("no-op", "read") or address in drifted:
            continue
        pending.append(Pending(address, name, change.get("action_reason", "")))
    return Report(stack, findings, pending, plan is not None, None, str(refresh.get("timestamp", "")))


def sorted_findings(findings: Sequence[Finding]) -> List[Finding]:
    return sorted(findings, key=lambda finding: (CATEGORY_ORDER.index(finding.category), finding.address))


def summary(report: Report) -> Dict[str, int]:
    counts = {category: 0 for category in CATEGORY_ORDER}
    for finding in report.findings:
        counts[finding.category] += 1
    counts["pending"] = len(report.pending)
    return counts


def exit_code(report: Report, strict: bool = False, fail_on_pending: bool = False) -> int:
    actionable = [finding for finding in report.findings if strict or finding.category != IGNORED]
    if actionable or (fail_on_pending and report.pending):
        return EXIT_DRIFT
    return EXIT_CLEAN


def dedupe_key(stack: str) -> str:
    return "driftwatch-" + hashlib.sha256(stack.encode("utf-8")).hexdigest()[:12]


def fingerprint(report: Report) -> str:
    signature = sorted([finding.address, finding.category, sorted(diff.path for diff in finding.diffs)] for finding in report.findings)
    if not signature:
        return "clean"
    return hashlib.sha256(json.dumps(signature).encode("utf-8")).hexdigest()[:16]


def headline(counts: Mapping[str, int]) -> str:
    drifted = counts[CHANGED] + counts[DELETED] + counts[REPLACE]
    if not drifted and not counts[IGNORED] and not counts["pending"]:
        return "no drift"
    first = f"{drifted} drifted"
    detail = [f"{counts[category]} {LABELS[category]}" for category in (REPLACE, DELETED, CHANGED) if counts[category]]
    if detail:
        first += f" ({', '.join(detail)})"
    return f"{first}, {counts[IGNORED]} ignored, {counts['pending']} unapplied change(s)"


def show_value(value: Any, sensitive: bool, limit: int = 60) -> str:
    if sensitive:
        return "(sensitive)"
    if value is None:
        return "null"
    text = json.dumps(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def md_code(value: str) -> str:
    text = value.replace("\n", " ").replace("|", "\\|")
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def render_text(report: Report, code: int) -> str:
    lines = [f"driftwatch: stack {report.stack}: {headline(summary(report))}"]
    for finding in sorted_findings(report.findings):
        lines.append(f"  {SYMBOLS[finding.category]:<3} {finding.address}  {LABELS[finding.category]}: {finding.reason}")
        for diff in finding.diffs:
            lines.append(f"        {diff.path}: {show_value(diff.before, diff.sensitive)} -> {show_value(diff.after, diff.sensitive)}")
    if report.pending:
        lines.append("unapplied changes (the configuration differs from state; not drift):")
        for item in report.pending:
            reason = f" ({item.reason})" if item.reason else ""
            lines.append(f"  {PENDING_SYMBOLS.get(item.action, '?'):<3} {item.address}  {item.action}{reason}")
    said = f"; tofu -detailed-exitcode said {report.tofu_exit}" if report.tofu_exit is not None else ""
    lines.append(f"exit {code}{said}")
    return "\n".join(lines)


def render_markdown(report: Report, code: int, limit: int = 80) -> str:
    lines = [f"## Drift report: {report.stack}", ""]
    if not report.findings and not report.pending:
        lines.append("No drift: every resource in state matches what its provider reports.")
    else:
        when = f" Plan taken {report.timestamp}." if report.timestamp else ""
        lines.append(f"**{headline(summary(report))}.** Exit code {code}.{when}")
    if report.findings:
        lines += ["", "| Address | Finding | Next apply | Changed attributes |", "|---|---|---|---|"]
        for finding in sorted_findings(report.findings):
            names = ", ".join(md_code(diff.path) for diff in finding.diffs[:5])
            if len(finding.diffs) > 5:
                names += f" and {len(finding.diffs) - 5} more"
            lines.append(f"| {md_code(finding.address)} | {LABELS[finding.category]} | {finding.next_apply or 'unknown'} | {names or 'the whole object'} |")
        for finding in sorted_findings(report.findings):
            lines += ["", f"### {md_code(finding.address)}: {LABELS[finding.category]}", "", finding.reason[0].upper() + finding.reason[1:] + "."]
            if finding.diffs:
                lines += ["", "| Attribute | Last applied | Now |", "|---|---|---|"]
                for diff in finding.diffs:
                    lines.append(f"| {md_code(diff.path)} | {md_code(show_value(diff.before, diff.sensitive, limit))} | {md_code(show_value(diff.after, diff.sensitive, limit))} |")
    if report.pending:
        lines += ["", "### Unapplied changes (not drift)", "", "The configuration differs from state for these. Nobody changed them outside OpenTofu; they wait for the next apply.", "", "| Address | Planned action | Reason |", "|---|---|---|"]
        for item in report.pending:
            lines.append(f"| {md_code(item.address)} | {item.action} | {item.reason or 'configuration change'} |")
    lines += ["", f"<!-- {dedupe_key(report.stack)} fingerprint={fingerprint(report)} -->"]
    return "\n".join(lines) + "\n"


def report_dict(report: Report, code: int) -> Dict[str, Any]:
    return {
        "stack": report.stack,
        "exit_code": code,
        "tofu_exit": report.tofu_exit,
        "key": dedupe_key(report.stack),
        "fingerprint": fingerprint(report),
        "full_plan": report.full_plan,
        "timestamp": report.timestamp,
        "summary": summary(report),
        "findings": [asdict(finding) for finding in sorted_findings(report.findings)],
        "pending": [asdict(item) for item in report.pending],
    }


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write_text(path: str, text: str, mode: str) -> None:
    with open(path, mode, encoding="utf-8") as handle:
        handle.write(text)


def build_report(args: argparse.Namespace, runner: Runner) -> Report:
    if args.command == "run":
        if not os.path.isdir(args.chdir):
            raise DriftError(f"--chdir {args.chdir}: no such directory")
        with tempfile.TemporaryDirectory(prefix="driftwatch-") as tmp:
            tofu_exit, refresh = run_plan(runner, args.tofu, args.chdir, os.path.join(tmp, "drift.tfplan"), True, args.lock_timeout)
            plan = None
            if not args.drift_only:
                _, plan = run_plan(runner, args.tofu, args.chdir, os.path.join(tmp, "next.tfplan"), False, args.lock_timeout)
        configs = args.config if args.config is not None else sorted(glob.glob(os.path.join(args.chdir, "*.tf.json")))
        stack = args.stack or os.path.basename(os.path.abspath(args.chdir))
    else:
        refresh = parse_plan(read_text(args.refresh_plan), args.refresh_plan)
        plan = parse_plan(read_text(args.plan), args.plan) if args.plan else None
        tofu_exit = None
        configs = args.config or []
        stack = args.stack or "default"
    report = classify(refresh, plan, load_ignore_rules(configs), stack)
    report.tofu_exit = tofu_exit
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="driftwatch", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run a refresh-only plan and a normal plan with tofu, then report")
    run.add_argument("--chdir", default=".", help="root module directory (already initialised with tofu init)")
    run.add_argument("--tofu", default=os.environ.get("DRIFTWATCH_TOFU", "tofu"), help="tofu or terraform binary")
    run.add_argument("--drift-only", action="store_true", help="skip the normal plan; faster, but the next-apply column stays unknown")
    run.add_argument("--lock-timeout", default="60s", help="how long to wait for the state lock")
    report = commands.add_parser("report", help="classify plan JSON you already saved")
    report.add_argument("--refresh-plan", required=True, help="output of tofu show -json on a refresh-only plan")
    report.add_argument("--plan", help="output of tofu show -json on a normal plan of the same root module")
    for sub in (run, report):
        sub.add_argument("--config", action="append", help="a .tf.json file to read lifecycle.ignore_changes from (repeatable)")
        sub.add_argument("--stack", help="name used in the report and in the dedupe key")
        sub.add_argument("--json", action="store_true", help="machine-readable report on stdout")
        sub.add_argument("--markdown", help="write the Markdown report to this file")
        sub.add_argument("--step-summary", action="store_true", help="append the Markdown report to $GITHUB_STEP_SUMMARY")
        sub.add_argument("--strict", action="store_true", help="ignored drift also exits 2")
        sub.add_argument("--fail-on-pending", action="store_true", help="unapplied changes also exit 2")
    return parser


def main(argv: Optional[Sequence[str]] = None, runner: Runner = subprocess_runner, environ: Optional[Mapping[str, str]] = None) -> int:
    environ = os.environ if environ is None else environ
    try:
        args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return EXIT_CLEAN if exc.code in (0, None) else EXIT_ERROR
    try:
        report = build_report(args, runner)
        code = exit_code(report, args.strict, args.fail_on_pending)
        markdown = render_markdown(report, code)
        if args.markdown:
            write_text(args.markdown, markdown, "w")
        if args.step_summary:
            target = environ.get("GITHUB_STEP_SUMMARY")
            if not target:
                raise DriftError("--step-summary needs GITHUB_STEP_SUMMARY, which GitHub Actions sets in every step")
            write_text(target, markdown, "a")
        print(json.dumps(report_dict(report, code), indent=2) if args.json else render_text(report, code))
        return code
    except (DriftError, OSError) as exc:
        print(f"driftwatch: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
