"""CloudCaptain capstone grader.

Scores a project folder milestone by milestone. The milestones, their points and
the pass score come from ../lesson.json; each milestone `some-id` is checked by
`check_some_id(project)` in checks.py, which returns (passed, detail).

    bash grader/grade.sh <project-dir>                 # every milestone
    bash grader/grade.sh <project-dir> --milestone ID  # only these (repeatable)
    bash grader/grade.sh <project-dir> --json          # machine-readable report

Exit 0: passed (score >= pass score, or every selected milestone passed).
Exit 1: not passed. Exit 2: usage error.

This file is the same in every capstone; put capstone logic in checks.py.
Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from types import ModuleType
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))


def load_lesson(path: Optional[str] = None) -> Dict[str, Any]:
    with open(path or os.path.join(HERE, "..", "lesson.json"), encoding="utf-8") as f:
        return json.load(f)


def load_checks(path: Optional[str] = None) -> ModuleType:
    path = path or os.path.join(HERE, "checks.py")
    spec = importlib.util.spec_from_file_location("capstone_checks", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_name(milestone_id: str) -> str:
    return "check_" + milestone_id.replace("-", "_")


def run_check(checks: ModuleType, milestone: Dict[str, Any], project: str) -> Dict[str, Any]:
    fn = getattr(checks, check_name(milestone["id"]), None)
    started = time.monotonic()
    if fn is None:
        passed, detail = False, f"checks.py has no {check_name(milestone['id'])}()"
    else:
        try:
            result = fn(project)
            passed, detail = bool(result[0]), str(result[1])
        except Exception as exc:  # a crashing check fails its milestone, not the grader
            passed, detail = False, f"check crashed: {type(exc).__name__}: {exc}"
    return {
        "id": milestone["id"],
        "title": milestone["title"],
        "points": milestone["points"],
        "earned": milestone["points"] if passed else 0,
        "passed": passed,
        "detail": detail,
        "seconds": round(time.monotonic() - started, 2),
    }


def grade(project: str, lesson: Dict[str, Any], checks: ModuleType, only: Optional[List[str]] = None) -> Dict[str, Any]:
    milestones = lesson["milestones"]
    known = [m["id"] for m in milestones]
    unknown = [m for m in (only or []) if m not in known]
    if unknown:
        raise ValueError(f"unknown milestone(s): {', '.join(unknown)} (known: {', '.join(known)})")
    selected = [m for m in milestones if not only or m["id"] in only]
    results = [run_check(checks, m, project) for m in selected]
    total = sum(m["points"] for m in milestones)
    score = sum(r["earned"] for r in results)
    passed = all(r["passed"] for r in results) if only else score >= lesson["pass_score"]
    return {
        "capstone": lesson["id"],
        "project": os.path.abspath(project),
        "score": score,
        "total": total,
        "pass_score": lesson["pass_score"],
        "partial": bool(only),
        "passed": passed,
        "milestones": results,
    }


def render_text(report: Dict[str, Any]) -> str:
    lines = [f"CloudCaptain grader: {report['capstone']} on {report['project']}"]
    for r in report["milestones"]:
        mark = "PASS" if r["passed"] else "FAIL"
        lines.append(f"  {mark}  {r['id']:<24} {r['earned']:>2}/{r['points']:<2}  {r['detail']}")
    if report["partial"]:
        lines.append(f"selected milestones: {'all passed' if report['passed'] else 'not all passed'}")
    else:
        verdict = "PASSED" if report["passed"] else "NOT PASSED"
        lines.append(f"score {report['score']}/{report['total']} (pass at {report['pass_score']}): {verdict}")
    return "\n".join(lines)


def render_markdown(report: Dict[str, Any]) -> str:
    rows = [f"| {'✅' if r['passed'] else '❌'} | {r['title']} | {r['earned']}/{r['points']} | {r['detail']} |" for r in report["milestones"]]
    head = f"### {report['capstone']}: {report['score']}/{report['total']} ({'passed' if report['passed'] else 'not passed'}, pass at {report['pass_score']})"
    return "\n".join([head, "", "| | Milestone | Points | Detail |", "|---|---|---|---|", *rows, ""])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="grade.sh", description="Grade a CloudCaptain capstone project.")
    parser.add_argument("project", help="the project folder to grade")
    parser.add_argument("--milestone", action="append", metavar="ID", help="grade only this milestone (repeatable)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.project):
        print(f"grade.sh: {args.project} is not a directory", file=sys.stderr)
        return 2
    try:
        report = grade(args.project, load_lesson(), load_checks(), args.milestone)
    except ValueError as exc:
        print(f"grade.sh: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.json else render_text(report))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(render_markdown(report) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
