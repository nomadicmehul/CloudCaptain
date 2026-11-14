from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List, Optional

from . import agent, evals, evidence, proposals, report, triage
from .config import Config, Refused, load_config
from .kube import Kube, KubeError

EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 3


def emit(data: Any) -> None:
    print(json.dumps(data, indent=2))


def cmd_collect(args: argparse.Namespace, config: Config, kube: Kube) -> int:
    emit(evidence.collect(kube, config.require(args.namespace)))
    return EXIT_OK


def cmd_triage(args: argparse.Namespace, config: Config, kube: Kube) -> int:
    emit(triage.triage(kube, config.require(args.namespace)))
    return EXIT_OK


def cmd_ask(args: argparse.Namespace, config: Config, kube: Kube) -> int:
    emit(agent.run_agent(args.question, config.require(args.namespace), config, args.max_turns, args.token_budget))
    return EXIT_OK


def cmd_apply(args: argparse.Namespace, config: Config, kube: Kube) -> int:
    emit(proposals.apply(kube, config, args.approve))
    return EXIT_OK


def cmd_report(args: argparse.Namespace, config: Config, kube: Kube) -> int:
    result = report.build_report(kube, config, config.require(args.namespace))
    print(json.dumps(result, indent=2) if args.format == "json" else report.render_markdown(result))
    return EXIT_OK


def cmd_eval(args: argparse.Namespace, config: Config, kube: Kube) -> int:
    with open(args.suite, encoding="utf-8") as handle:
        suite = json.load(handle)
    emit(evals.run_suite(suite, args.trials, config.model_url_set))
    return EXIT_OK


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="copilot", description="An on-call copilot for one Kubernetes namespace.")
    sub = root.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="collect evidence of unhealthy workloads")
    collect.add_argument("--namespace", required=True)
    collect.add_argument("--json", action="store_true")
    collect.set_defaults(handler=cmd_collect)
    tri = sub.add_parser("triage", help="classify every workload with deterministic rules")
    tri.add_argument("--namespace", required=True)
    tri.add_argument("--json", action="store_true")
    tri.set_defaults(handler=cmd_triage)
    ask = sub.add_parser("ask", help="investigate with the model and read-only tools")
    ask.add_argument("--namespace", required=True)
    ask.add_argument("--max-turns", type=int, default=8)
    ask.add_argument("--token-budget", type=int, default=200000)
    ask.add_argument("--json", action="store_true")
    ask.add_argument("question")
    ask.set_defaults(handler=cmd_ask)
    apply = sub.add_parser("apply", help="run one approved proposal")
    apply.add_argument("--approve", required=True, metavar="PROPOSAL_ID")
    apply.add_argument("--json", action="store_true")
    apply.set_defaults(handler=cmd_apply)
    rep = sub.add_parser("report", help="write the incident report")
    rep.add_argument("--namespace", required=True)
    rep.add_argument("--format", choices=["json", "md"], default="md")
    rep.set_defaults(handler=cmd_report)
    ev = sub.add_parser("eval", help="run an eval suite of recorded cases")
    ev.add_argument("suite")
    ev.add_argument("--trials", type=int)
    ev.add_argument("--json", action="store_true")
    ev.set_defaults(handler=cmd_eval)
    return root


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    config = load_config()
    kube = Kube(config.context)
    try:
        return args.handler(args, config, kube)
    except NotImplementedError as exc:
        print(f"copilot: not implemented: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Refused as exc:
        print(f"copilot: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (KubeError, agent.ModelError, ValueError, OSError) as exc:
        print(f"copilot: {exc}", file=sys.stderr)
        return EXIT_ERROR
