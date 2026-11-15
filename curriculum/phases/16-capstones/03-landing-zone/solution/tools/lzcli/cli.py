from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from types import ModuleType
from typing import Any, Dict, Optional, Sequence

from . import drift
from .tofu import LzError, Runner, Tofu, subprocess_runner

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_gate(root: str) -> ModuleType:
    path = os.path.join(root, "policy", "gate.py")
    spec = importlib.util.spec_from_file_location("lz_policy_gate", path)
    if spec is None or spec.loader is None:
        raise LzError(f"cannot load {path}", 2)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def count_changes(plan: Dict[str, Any]) -> Dict[str, int]:
    counts = {"create": 0, "update": 0, "delete": 0}
    for change in plan.get("resource_changes") or []:
        for action in (change.get("change") or {}).get("actions") or []:
            if action in counts:
                counts[action] += 1
    return counts


def emit(payload: Dict[str, Any], as_json: bool, text: str) -> None:
    print(json.dumps(payload, indent=2) if as_json else text)


def cmd_plan(args: argparse.Namespace, tofu: Tofu) -> int:
    result = tofu.plan(args.env)
    payload = {"env": args.env, "plan_file": result["plan_file"], "plan_json": result["plan_json"], "changes": count_changes(result["plan"])}
    emit(payload, args.json, f"plan for envs/{args.env}: {payload['changes']}; plan JSON written to {result['plan_json']}")
    return 0


def cmd_apply(args: argparse.Namespace, tofu: Tofu) -> int:
    outputs = tofu.apply(args.env)
    emit({"env": args.env, "outputs": outputs}, args.json, f"applied envs/{args.env}: {len(outputs)} output(s)")
    return 0


def cmd_drift(args: argparse.Namespace, tofu: Tofu) -> int:
    result = tofu.plan(args.env, refresh_only=True)
    report = drift.drift_report(args.env, result["plan"])
    lines = [f"envs/{args.env}: {'drift' if report['drifted'] else 'no drift'}"]
    lines += [f"  {r['address']} ({r['id']}): {', '.join(r['attributes'])}" for r in report["resources"]]
    emit(report, args.json, "\n".join(lines))
    return 2 if report["drifted"] else 0


def cmd_policy(args: argparse.Namespace, root: str) -> int:
    gate = load_gate(root)
    try:
        with open(args.plan, encoding="utf-8") as handle:
            plan = json.load(handle)
        violations = gate.evaluate(plan)
    except (OSError, ValueError, gate.PlanError) as error:
        raise LzError(f"cannot evaluate {args.plan}: {error}", 2) from None
    report = gate.report(violations)
    lines = [f"{v.rule:<16} {v.address}: {v.message}" for v in violations] or ["no violations"]
    emit(report, args.json, "\n".join(lines))
    return 1 if violations else 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="lz", description="Plan, apply, gate and watch the landing zone.")
    sub = top.add_subparsers(dest="command", required=True)
    for name, help_text in (("plan", "plan an env and write the plan JSON"), ("apply", "apply an env"), ("drift", "report changes made outside OpenTofu")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("env")
        command.add_argument("--json", action="store_true")
    policy = sub.add_parser("policy", help="check a plan JSON against the landing zone rules")
    policy.add_argument("plan")
    policy.add_argument("--json", action="store_true")
    return top


def main(argv: Optional[Sequence[str]] = None, runner: Runner = subprocess_runner, root: str = ROOT, environ: Optional[Dict[str, str]] = None) -> int:
    args = parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "policy":
            return cmd_policy(args, root)
        tofu = Tofu(root, environ, runner)
        handlers = {"plan": cmd_plan, "apply": cmd_apply, "drift": cmd_drift}
        return handlers[args.command](args, tofu)
    except LzError as error:
        if getattr(args, "json", False):
            print(json.dumps(dict({"error": "failed", "message": str(error)}, **error.payload), indent=2))
        print(f"lz: {error}", file=sys.stderr)
        return error.code

