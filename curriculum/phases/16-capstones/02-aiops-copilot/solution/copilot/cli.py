from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List, Optional

from . import agent, evals, evidence, proposals, report, triage
from .config import Config, Refused, load_config
from .kube import Kube, KubeError
from .tools import ToolBox

EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 3


def emit(data: Any, as_json: bool, text: str) -> None:
    print(json.dumps(data, indent=2) if as_json else text)


def cmd_collect(args: argparse.Namespace, config: Config, kube: Kube) -> int:
    namespace = config.require(args.namespace)
    result = evidence.collect(kube, namespace)
    emit(result, args.json, "\n".join(f"{e['id']:<60} {e['signal']:<18} {e['summary']}" for e in result["evidence"]) or f"no evidence of trouble in {namespace}")
    return EXIT_OK


def cmd_triage(args: argparse.Namespace, config: Config, kube: Kube) -> int:
    namespace = config.require(args.namespace)
    result = triage.triage(kube, namespace)
    emit({"namespace": namespace, "incidents": result["incidents"]}, args.json, "\n".join(f"{i['workload']:<32} {i['category']:<18} {', '.join(i['citations'])}" for i in result["incidents"]))
    return EXIT_OK


def cmd_ask(args: argparse.Namespace, config: Config, kube: Kube) -> int:
    namespace = config.require(args.namespace)
    toolbox = ToolBox(kube, config, proposals.ProposalStore(config.home))
    result = agent.run_agent(args.question, namespace, config, toolbox, agent.http_model(config), args.max_turns, args.token_budget)
    lines = [result.answer or "(no answer)", "", f"stop: {result.stop_reason} after {result.turns} turn(s), {len(result.tool_calls)} tool call(s)"]
    lines += [f"proposal {p['id']}: {p['action']} {p['target']} (risk {p['risk']}); approve with copilot apply --approve {p['id']}" for p in result.proposals]
    emit(result.to_dict(), args.json, "\n".join(lines))
    return EXIT_OK


def cmd_apply(args: argparse.Namespace, config: Config, kube: Kube) -> int:
    proposal = proposals.apply(kube, proposals.ProposalStore(config.home), config, args.approve)
    emit(proposal, args.json, f"{proposal['id']} {proposal['status']}: {proposal.get('result', '')}")
    return EXIT_OK if proposal["status"] == "applied" else EXIT_ERROR


def cmd_report(args: argparse.Namespace, config: Config, kube: Kube) -> int:
    namespace = config.require(args.namespace)
    result = report.build_report(kube, proposals.ProposalStore(config.home), namespace)
    print(json.dumps(result, indent=2) if args.format == "json" else report.render_markdown(result), end="" if args.format == "md" else "\n")
    return EXIT_OK


def cmd_eval(args: argparse.Namespace, config: Config, kube: Kube) -> int:
    suite = evals.load_suite(args.suite)
    model = evals.agent_verdict(agent.http_model(config), config.model, config.effort) if config.model_url_set else None
    result = evals.run_suite(suite, args.trials, model)
    emit(result, args.json, evals.render(result))
    if args.min_pass_rate is not None and result["summary"]["pass_rate"] < args.min_pass_rate:
        return EXIT_ERROR
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
    ev.add_argument("--min-pass-rate", type=float)
    ev.add_argument("--json", action="store_true")
    ev.set_defaults(handler=cmd_eval)
    return root


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    config = load_config()
    kube = Kube(config.context)
    try:
        return args.handler(args, config, kube)
    except Refused as exc:
        print(f"copilot: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (KubeError, agent.ModelError, ValueError, OSError) as exc:
        print(f"copilot: {exc}", file=sys.stderr)
        return EXIT_ERROR
