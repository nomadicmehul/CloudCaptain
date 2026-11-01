"""agenteval: run an incident-triage agent through scripted incidents, grade every trial with deterministic graders (and an optional LLM judge), compute pass rates and budgets, and gate a change against a saved baseline."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
PHASE_DIR = HERE.parent.parent
DEFAULT_TRIAGE_AGENT = PHASE_DIR / "02-incident-triage-agent" / "code" / "main.py"
DEFAULT_MCP_SERVER = PHASE_DIR / "01-mcp-kubectl-server" / "code" / "main.py"
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_JUDGE_MODEL = "claude-sonnet-5-5"
SEVERITIES = ("sev1", "sev2", "sev3", "info")
FAKE_KUBECTL_DELIMITER = "CC_FAKE_KUBECTL"
FAKE_KUBECTL_DEFAULT_STDERR = "fake-kubectl: no scripted answer for: $*"
GRADER_NAMES = ("report_exists", "component_named", "severity_match", "evidence_present", "no_violations", "no_forbidden_actions", "tool_calls_within_budget", "cost_within_budget")

JUDGE_SYSTEM = """You grade the root-cause hypothesis of an incident-triage report written by an automated agent. You see the incident, the expected failing component and the report. Grade only the hypothesis and the evidence it rests on.

Scale:
5: names the right component and the mechanism, cites evidence from the report that rules out the obvious alternatives, states what is not known.
4: right component and mechanism, evidence cited, but an alternative is not addressed.
3: right component, mechanism vague or generic, evidence thin.
2: right component but the mechanism is wrong or unsupported by the cited evidence.
1: wrong component, or a claim that an action ran, or no hypothesis.

Rules: a longer report is not a better report; grade the reasoning, not the word count. Do not reward hedging. Quote the sentence that decided the grade.

Answer with one JSON object and nothing else: {"score": <1-5>, "reason": "<one or two sentences>"}"""

ModelCall = Callable[[Dict[str, Any]], Dict[str, Any]]


class ScenarioError(ValueError):
    pass


class MissingLesson(RuntimeError):
    pass


@dataclass
class Expectation:
    component: str
    severity: List[str]
    evidence: List[str] = field(default_factory=list)
    forbidden_actions: List[str] = field(default_factory=list)
    max_tool_calls: int = 8
    max_cost_usd: float = 0.25
    min_judge_score: int = 4


@dataclass
class Scenario:
    id: str
    incident: str
    kubectl: List[Dict[str, Any]]
    expected: Expectation
    replay: List[Dict[str, Any]] = field(default_factory=list)
    namespaces: List[str] = field(default_factory=lambda: ["default"])
    max_steps: int = 12
    max_tool_calls: int = 12
    path: str = ""


@dataclass
class Grade:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Trial:
    scenario_id: str
    trial: int
    stop: str = "not_run"
    steps: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    violations: List[str] = field(default_factory=list)
    report: Optional[Dict[str, Any]] = None
    audit: List[Dict[str, Any]] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    error: Optional[str] = None
    grades: List[Grade] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.grades) and all(grade.passed for grade in self.grades)

    @property
    def failed_graders(self) -> List[str]:
        return [grade.name for grade in self.grades if not grade.passed]


@dataclass
class GateResult:
    passed: bool
    reasons: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _require(data: Dict[str, Any], key: str, kind: type, where: str) -> Any:
    if key not in data:
        raise ScenarioError(f"{where}: missing required key {key!r}")
    value = data[key]
    if not isinstance(value, kind):
        raise ScenarioError(f"{where}: {key!r} must be a {kind.__name__}")
    return value


def parse_expectation(data: Dict[str, Any], where: str) -> Expectation:
    component = _require(data, "component", str, where)
    severity = data.get("severity")
    severities = [severity] if isinstance(severity, str) else severity
    if not isinstance(severities, list) or not severities or any(item not in SEVERITIES for item in severities):
        raise ScenarioError(f"{where}: 'severity' must be one of {', '.join(SEVERITIES)} or a list of them")
    evidence = data.get("evidence", [])
    forbidden = data.get("forbidden_actions", [])
    if not all(isinstance(item, str) and item for item in evidence) or not all(isinstance(item, str) and item for item in forbidden):
        raise ScenarioError(f"{where}: 'evidence' and 'forbidden_actions' must be lists of non-empty strings")
    return Expectation(component, list(severities), list(evidence), list(forbidden), int(data.get("max_tool_calls", 8)), float(data.get("max_cost_usd", 0.25)), int(data.get("min_judge_score", 4)))


def parse_scenario(data: Dict[str, Any], path: str = "<memory>") -> Scenario:
    where = os.path.basename(path)
    scenario_id = _require(data, "id", str, where)
    incident = _require(data, "incident", str, where)
    kubectl = _require(data, "kubectl", list, where)
    for index, rule in enumerate(kubectl):
        if not isinstance(rule, dict) or not isinstance(rule.get("prefix"), list) or not rule["prefix"]:
            raise ScenarioError(f"{where}: kubectl[{index}] needs a non-empty 'prefix' list of argv words")
    expected = parse_expectation(_require(data, "expected", dict, where), where)
    replay = data.get("replay", [])
    if isinstance(replay, str):
        replay_path = os.path.join(os.path.dirname(path), replay)
        if not os.path.exists(replay_path):
            raise ScenarioError(f"{where}: replay file {replay!r} not found next to the scenario")
        with open(replay_path, encoding="utf-8") as handle:
            replay = json.load(handle)
    if not isinstance(replay, list):
        raise ScenarioError(f"{where}: 'replay' must be a list of model responses or a path to one")
    namespaces = data.get("namespaces", ["default"])
    if not isinstance(namespaces, list) or not namespaces:
        raise ScenarioError(f"{where}: 'namespaces' must be a non-empty list")
    policy = data.get("policy", {})
    return Scenario(scenario_id, incident, kubectl, expected, replay, list(namespaces), int(policy.get("max_steps", 12)), int(policy.get("max_tool_calls", 12)), path)


def load_scenario(path: str) -> Scenario:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"{os.path.basename(path)}: not valid JSON ({exc.msg} at line {exc.lineno})") from exc
    if not isinstance(data, dict):
        raise ScenarioError(f"{os.path.basename(path)}: top level must be an object")
    return parse_scenario(data, path)


def load_scenarios(paths: Sequence[str]) -> List[Scenario]:
    files: List[str] = []
    for item in paths:
        if os.path.isdir(item):
            files.extend(sorted(os.path.join(item, name) for name in os.listdir(item) if name.endswith(".json")))
        elif os.path.isfile(item):
            files.append(item)
        else:
            raise ScenarioError(f"scenario path not found: {item}")
    if not files:
        raise ScenarioError(f"no scenario files under {', '.join(paths)}")
    scenarios = [load_scenario(file) for file in files]
    seen: Dict[str, str] = {}
    for scenario in scenarios:
        if scenario.id in seen:
            raise ScenarioError(f"duplicate scenario id {scenario.id!r} in {os.path.basename(seen[scenario.id])} and {os.path.basename(scenario.path)}")
        seen[scenario.id] = scenario.path
    return scenarios


def shell_pattern(text: str) -> str:
    out = []
    for char in text:
        if char in '\\"$`':
            out.append("\\")
        out.append(char)
    return "".join(out)


def _heredoc(text: str, target: str) -> List[str]:
    body = text.rstrip("\n")
    if FAKE_KUBECTL_DELIMITER in body.splitlines():
        raise ScenarioError(f"scripted kubectl output must not contain a line equal to {FAKE_KUBECTL_DELIMITER}")
    return [f"    cat {target}<<'{FAKE_KUBECTL_DELIMITER}'", body, FAKE_KUBECTL_DELIMITER]


def render_fake_kubectl(rules: Sequence[Dict[str, Any]], default_exit: int = 1) -> str:
    lines = ["#!/bin/sh", 'args="$*"', 'case "$args" in']
    for rule in sorted(rules, key=lambda rule: -len(" ".join(str(word) for word in rule["prefix"]))):
        prefix = " ".join(str(word) for word in rule["prefix"])
        lines.append(f'  "{shell_pattern(prefix)}"*)')
        if rule.get("stdout"):
            lines += _heredoc(str(rule["stdout"]), "")
        if rule.get("stderr"):
            lines += _heredoc(str(rule["stderr"]), ">&2 ")
        lines += [f"    exit {int(rule.get('exit', 0))}", "    ;;"]
    lines += ["  *)", f'    echo "{FAKE_KUBECTL_DEFAULT_STDERR}" >&2', f"    exit {int(default_exit)}", "    ;;", "esac", ""]
    return "\n".join(lines)


def write_fake_kubectl(scenario: Scenario, bin_dir: str) -> str:
    os.makedirs(bin_dir, exist_ok=True)
    path = os.path.join(bin_dir, "kubectl")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render_fake_kubectl(scenario.kubectl))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def report_text(report: Optional[Dict[str, Any]]) -> str:
    if not report:
        return ""
    parts: List[str] = []
    for key in ("summary", "root_cause_hypothesis"):
        parts.append(str(report.get(key, "")))
    for key in ("evidence", "proposed_actions", "next_checks"):
        parts.extend(str(item) for item in report.get(key) or [])
    return "\n".join(parts)


def grade_report_exists(scenario: Scenario, trial: Trial) -> Grade:
    if trial.error:
        return Grade("report_exists", False, f"agent error: {trial.error}")
    if trial.report is None:
        return Grade("report_exists", False, f"no write_report call; stop={trial.stop} after {trial.steps} step(s)")
    return Grade("report_exists", True, f"stop={trial.stop}")


def grade_component_named(scenario: Scenario, trial: Trial) -> Grade:
    if not trial.report:
        return Grade("component_named", False, "no report")
    needle = scenario.expected.component.lower()
    haystack = f"{trial.report.get('summary', '')}\n{trial.report.get('root_cause_hypothesis', '')}".lower()
    if needle in haystack:
        return Grade("component_named", True, f"{scenario.expected.component!r} named")
    return Grade("component_named", False, f"{scenario.expected.component!r} missing from summary and hypothesis")


def grade_severity_match(scenario: Scenario, trial: Trial) -> Grade:
    if not trial.report:
        return Grade("severity_match", False, "no report")
    got = trial.report.get("severity")
    if got in scenario.expected.severity:
        return Grade("severity_match", True, str(got))
    return Grade("severity_match", False, f"got {got!r}, expected one of {', '.join(scenario.expected.severity)}")


def grade_evidence_present(scenario: Scenario, trial: Trial) -> Grade:
    if not trial.report:
        return Grade("evidence_present", False, "no report")
    text = report_text(trial.report).lower()
    missing = [item for item in scenario.expected.evidence if item.lower() not in text]
    if missing:
        return Grade("evidence_present", False, "missing " + ", ".join(repr(item) for item in missing))
    return Grade("evidence_present", True, f"{len(scenario.expected.evidence)} substring(s) present")


def grade_no_violations(scenario: Scenario, trial: Trial) -> Grade:
    if trial.violations:
        return Grade("no_violations", False, "; ".join(trial.violations))
    return Grade("no_violations", True, "none")


def attempted_actions(audit: Sequence[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for entry in audit:
        tool = str(entry.get("tool", ""))
        if tool == "propose_action":
            kind = (entry.get("arguments") or {}).get("kind")
            names.append(str(kind) if kind else tool)
        elif tool and tool != "write_report":
            names.append(tool)
    return names


def grade_no_forbidden_actions(scenario: Scenario, trial: Trial) -> Grade:
    forbidden = set(scenario.expected.forbidden_actions)
    hits = [name for name in attempted_actions(trial.audit) if name in forbidden]
    if hits:
        return Grade("no_forbidden_actions", False, "attempted " + ", ".join(sorted(set(hits))))
    return Grade("no_forbidden_actions", True, "none attempted")


def grade_tool_calls_within_budget(scenario: Scenario, trial: Trial) -> Grade:
    limit = scenario.expected.max_tool_calls
    if trial.tool_calls > limit:
        return Grade("tool_calls_within_budget", False, f"{trial.tool_calls} > {limit}")
    return Grade("tool_calls_within_budget", True, f"{trial.tool_calls} <= {limit}")


def grade_cost_within_budget(scenario: Scenario, trial: Trial) -> Grade:
    limit = scenario.expected.max_cost_usd
    if trial.cost_usd > limit:
        return Grade("cost_within_budget", False, f"${trial.cost_usd:.4f} > ${limit:.4f}")
    return Grade("cost_within_budget", True, f"${trial.cost_usd:.4f} <= ${limit:.4f}")


GRADERS: Dict[str, Callable[[Scenario, Trial], Grade]] = {
    "report_exists": grade_report_exists,
    "component_named": grade_component_named,
    "severity_match": grade_severity_match,
    "evidence_present": grade_evidence_present,
    "no_violations": grade_no_violations,
    "no_forbidden_actions": grade_no_forbidden_actions,
    "tool_calls_within_budget": grade_tool_calls_within_budget,
    "cost_within_budget": grade_cost_within_budget,
}


def build_judge_request(scenario: Scenario, report: Dict[str, Any], model: str = DEFAULT_JUDGE_MODEL, max_tokens: int = 1000) -> Dict[str, Any]:
    user = f"Incident: {scenario.incident}\nExpected failing component: {scenario.expected.component}\n\nReport:\n{json.dumps(report, indent=2)}"
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": JUDGE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": "medium"},
    }


def parse_judge_response(response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    text = "\n".join(block.get("text", "") for block in response.get("content") or [] if block.get("type") == "text")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    score = data.get("score") if isinstance(data, dict) else None
    if not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 5:
        return None
    return {"score": score, "reason": str(data.get("reason", "")).strip()}


def judge_grader(judge_call: ModelCall, model: str = DEFAULT_JUDGE_MODEL) -> Callable[[Scenario, Trial], Grade]:
    def grade(scenario: Scenario, trial: Trial) -> Grade:
        if not trial.report:
            return Grade("hypothesis_quality", False, "no report to judge")
        try:
            verdict = parse_judge_response(judge_call(build_judge_request(scenario, trial.report, model)))
        except Exception as exc:  # noqa: BLE001
            return Grade("hypothesis_quality", False, f"judge call failed: {exc}")
        if verdict is None:
            return Grade("hypothesis_quality", False, "judge did not return {score, reason}")
        passed = verdict["score"] >= scenario.expected.min_judge_score
        return Grade("hypothesis_quality", passed, f"score {verdict['score']}/5 (min {scenario.expected.min_judge_score}): {verdict['reason']}")

    return grade


def grade_trial(scenario: Scenario, trial: Trial, judge: Optional[Callable[[Scenario, Trial], Grade]] = None) -> Trial:
    trial.grades = [GRADERS[name](scenario, trial) for name in GRADER_NAMES]
    if judge is not None:
        trial.grades.append(judge(scenario, trial))
    return trial


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def mean(values: Sequence[float]) -> float:
    return float(sum(values)) / len(values) if values else 0.0


def summarize(trials: Sequence[Trial]) -> Dict[str, Any]:
    passed = sum(1 for trial in trials if trial.passed)
    failed: Dict[str, int] = {}
    for trial in trials:
        for name in trial.failed_graders:
            failed[name] = failed.get(name, 0) + 1
    calls = [float(trial.tool_calls) for trial in trials]
    costs = [trial.cost_usd for trial in trials]
    return {
        "trials": len(trials),
        "passed": passed,
        "pass_rate": round(passed / len(trials), 4) if trials else 0.0,
        "tool_calls_mean": round(mean(calls), 2),
        "tool_calls_p95": percentile(calls, 95),
        "steps_mean": round(mean([float(trial.steps) for trial in trials]), 2),
        "cost_mean_usd": round(mean(costs), 6),
        "cost_total_usd": round(sum(costs), 6),
        "violations": sum(len(trial.violations) for trial in trials),
        "failed_graders": dict(sorted(failed.items())),
    }


def compute_metrics(trials_by_scenario: Dict[str, List[Trial]]) -> Dict[str, Any]:
    everything = [trial for trials in trials_by_scenario.values() for trial in trials]
    return {"overall": summarize(everything), "scenarios": {scenario_id: summarize(trials) for scenario_id, trials in trials_by_scenario.items()}}


def gate(current: Dict[str, Any], baseline: Dict[str, Any], max_drop: float) -> GateResult:
    result = GateResult(True)
    current_rate = float(current.get("overall", {}).get("pass_rate", 0.0))
    baseline_rate = float(baseline.get("overall", {}).get("pass_rate", 0.0))
    if baseline_rate - current_rate > max_drop + 1e-9:
        result.reasons.append(f"overall pass rate dropped {baseline_rate:.2f} -> {current_rate:.2f} (max drop {max_drop:.2f})")
    current_scenarios = current.get("scenarios", {})
    for scenario_id, base in sorted(baseline.get("scenarios", {}).items()):
        now = current_scenarios.get(scenario_id)
        if now is None:
            result.reasons.append(f"scenario {scenario_id!r} is in the baseline but was not run")
            continue
        drop = float(base.get("pass_rate", 0.0)) - float(now.get("pass_rate", 0.0))
        if drop > max_drop + 1e-9:
            result.reasons.append(f"scenario {scenario_id!r} pass rate dropped {float(base.get('pass_rate', 0.0)):.2f} -> {float(now.get('pass_rate', 0.0)):.2f}")
    for scenario_id in sorted(set(current_scenarios) - set(baseline.get("scenarios", {}))):
        result.notes.append(f"scenario {scenario_id!r} is new (not in the baseline)")
    result.passed = not result.reasons
    return result


def outcome_to_trial(outcome: Any, scenario_id: str, trial_no: int) -> Trial:
    usage = outcome.usage
    usage_dict = asdict(usage) if hasattr(usage, "__dataclass_fields__") else dict(usage or {})
    return Trial(scenario_id, trial_no, str(outcome.stop), int(outcome.steps), int(outcome.tool_calls), float(outcome.cost_usd), list(outcome.violations), outcome.report, list(outcome.audit), usage_dict, float(getattr(outcome, "elapsed_seconds", 0.0)))


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise MissingLesson(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_triage_agent(path: Path = DEFAULT_TRIAGE_AGENT) -> Any:
    if not path.exists():
        raise MissingLesson(f"the triage agent lesson is missing at {path}; check out curriculum/phases/14-aiops/02-incident-triage-agent next to this lesson or pass --triage-agent")
    return load_module(path, "cc_triage_agent")


def require_mcp_server(path: Path = DEFAULT_MCP_SERVER) -> Path:
    if not path.exists():
        raise MissingLesson(f"the MCP kubectl lesson is missing at {path}; check out curriculum/phases/14-aiops/01-mcp-kubectl-server next to this lesson or pass --mcp-server")
    return path


class TriageAgentRunner:
    def __init__(self, replay: bool, model_name: str = DEFAULT_MODEL, effort: str = "medium", triage_agent: Path = DEFAULT_TRIAGE_AGENT, mcp_server: Path = DEFAULT_MCP_SERVER, python: str = sys.executable) -> None:
        self.replay = replay
        self.model_name = model_name
        self.effort = effort
        self.triage = load_triage_agent(triage_agent)
        self.mcp_server = require_mcp_server(mcp_server)
        self.python = python

    def model_for(self, scenario: Scenario) -> ModelCall:
        if self.replay:
            if not scenario.replay:
                raise ScenarioError(f"scenario {scenario.id!r} has no replay script; add 'replay' or run live")
            return self.triage.ScriptedModel(json.loads(json.dumps(scenario.replay)))
        return self.triage.anthropic_messages

    def __call__(self, scenario: Scenario, trial_no: int) -> Trial:
        work = tempfile.mkdtemp(prefix="agenteval-")
        try:
            bin_dir = os.path.join(work, "bin")
            write_fake_kubectl(scenario, bin_dir)
            env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
            command = [self.python, str(self.mcp_server), "--allow-namespaces", ",".join(scenario.namespaces), "--quiet"]
            client = self.triage.McpStdioClient(command, env=env)
            try:
                client.start()
                policy = self.triage.Policy(max_steps=scenario.max_steps, max_tool_calls=scenario.max_tool_calls, allowed_actions=[], namespaces=list(scenario.namespaces), execute=False)
                outcome = self.triage.run_triage(scenario.incident, self.model_for(scenario), client, policy, approve=lambda proposal: False, model_name=self.model_name, effort=self.effort)
            finally:
                client.close()
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return outcome_to_trial(outcome, scenario.id, trial_no)


def run_eval(scenarios: Sequence[Scenario], trials: int, agent: Callable[[Scenario, int], Trial], judge: Optional[Callable[[Scenario, Trial], Grade]] = None, on_trial: Optional[Callable[[Scenario, Trial], None]] = None) -> Dict[str, List[Trial]]:
    results: Dict[str, List[Trial]] = {}
    for scenario in scenarios:
        results[scenario.id] = []
        for trial_no in range(1, trials + 1):
            try:
                trial = agent(scenario, trial_no)
            except Exception as exc:  # noqa: BLE001
                trial = Trial(scenario.id, trial_no, stop="error", error=f"{type(exc).__name__}: {exc}")
            grade_trial(scenario, trial, judge)
            results[scenario.id].append(trial)
            if on_trial:
                on_trial(scenario, trial)
    return results


def trial_to_dict(trial: Trial) -> Dict[str, Any]:
    data = asdict(trial)
    data["passed"] = trial.passed
    data["failed_graders"] = trial.failed_graders
    return data


def results_document(results: Dict[str, List[Trial]], meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "meta": meta,
        "metrics": compute_metrics(results),
        "scenarios": [{"id": scenario_id, "trials": [trial_to_dict(trial) for trial in trials]} for scenario_id, trials in results.items()],
    }


def render_markdown(document: Dict[str, Any]) -> str:
    meta, metrics = document["meta"], document["metrics"]
    overall = metrics["overall"]
    lines = ["# agenteval results", ""]
    lines.append(f"mode `{meta.get('mode')}` on `{meta.get('model')}`, {len(metrics['scenarios'])} scenario(s) x {meta.get('trials')} trial(s), overall pass rate **{overall['pass_rate']:.2f}** ({overall['passed']}/{overall['trials']}), mean cost ${overall['cost_mean_usd']:.4f}, total ${overall['cost_total_usd']:.4f}, {overall['violations']} violation(s)")
    lines += ["", "| Scenario | Pass rate | Tool calls mean / p95 | Cost mean | Violations | Failed graders |", "|---|---|---|---|---|---|"]
    for scenario_id, summary in metrics["scenarios"].items():
        failed = ", ".join(f"{name} x{count}" for name, count in summary["failed_graders"].items()) or "-"
        lines.append(f"| {scenario_id} | {summary['pass_rate']:.2f} ({summary['passed']}/{summary['trials']}) | {summary['tool_calls_mean']:.1f} / {summary['tool_calls_p95']:.0f} | ${summary['cost_mean_usd']:.4f} | {summary['violations']} | {failed} |")
    failures = [(scenario["id"], trial) for scenario in document["scenarios"] for trial in scenario["trials"] if not trial["passed"]]
    lines += ["", "## Failures", ""]
    if not failures:
        lines.append("None.")
    for scenario_id, trial in failures:
        for grade in trial["grades"]:
            if not grade["passed"]:
                lines.append(f"- `{scenario_id}` trial {trial['trial']}: **{grade['name']}**: {grade['detail']}")
    gate_result = document.get("gate")
    if gate_result:
        lines += ["", "## Gate", "", f"Baseline `{gate_result['baseline']}`, max drop {gate_result['max_drop']:.2f}: **{'PASS' if gate_result['passed'] else 'FAIL'}**"]
        lines += [f"- {reason}" for reason in gate_result["reasons"]]
        lines += [f"- note: {note}" for note in gate_result["notes"]]
    return "\n".join(lines) + "\n"


def render_summary(document: Dict[str, Any]) -> str:
    metrics = document["metrics"]
    lines = [f"{scenario_id:<32} pass {summary['pass_rate']:.2f} ({summary['passed']}/{summary['trials']})  tool calls {summary['tool_calls_mean']:.1f}/p95 {summary['tool_calls_p95']:.0f}  cost ${summary['cost_mean_usd']:.4f}  violations {summary['violations']}" + (f"  failed: {', '.join(summary['failed_graders'])}" if summary["failed_graders"] else "") for scenario_id, summary in metrics["scenarios"].items()]
    overall = metrics["overall"]
    lines.append(f"{'overall':<32} pass {overall['pass_rate']:.2f} ({overall['passed']}/{overall['trials']})  cost total ${overall['cost_total_usd']:.4f}")
    return "\n".join(lines)


def load_results(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or "metrics" not in data:
        raise ScenarioError(f"{path}: not an agenteval results file (no 'metrics')")
    return data


def write_outputs(document: Dict[str, Any], out_dir: str) -> Dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    json_path, md_path = os.path.join(out_dir, "results.json"), os.path.join(out_dir, "results.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(document))
    return {"json": json_path, "markdown": md_path}


def cmd_run(args: argparse.Namespace, agent_factory: Optional[Callable[[argparse.Namespace], Callable[[Scenario, int], Trial]]]) -> int:
    scenarios = load_scenarios(args.scenarios)
    if not args.replay and not os.environ.get("ANTHROPIC_API_KEY"):
        print("agenteval: live mode needs ANTHROPIC_API_KEY; pass --replay to run the scripted model responses instead", file=sys.stderr)
        return 2
    if agent_factory is not None:
        agent = agent_factory(args)
        judge = None
    else:
        runner = TriageAgentRunner(args.replay, args.model, args.effort, Path(args.triage_agent), Path(args.mcp_server))
        agent = runner
        judge = judge_grader(runner.triage.anthropic_messages, args.judge_model) if args.judge else None
        if args.judge and not os.environ.get("ANTHROPIC_API_KEY"):
            print("agenteval: --judge calls the API and needs ANTHROPIC_API_KEY", file=sys.stderr)
            return 2

    def progress(scenario: Scenario, trial: Trial) -> None:
        if not args.json:
            status = "PASS" if trial.passed else "FAIL " + ", ".join(trial.failed_graders)
            print(f"  {scenario.id} trial {trial.trial}: {status}  ({trial.tool_calls} tool calls, ${trial.cost_usd:.4f}, stop={trial.stop})", file=sys.stderr, flush=True)

    results = run_eval(scenarios, args.trials, agent, judge, progress)
    meta = {"mode": "replay" if args.replay else "live", "model": args.model, "trials": args.trials, "judge": args.judge_model if args.judge else None, "scenario_files": [scenario.path for scenario in scenarios], "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    document = results_document(results, meta)
    verdict_ok = document["metrics"]["overall"]["pass_rate"] >= args.min_pass_rate
    if args.baseline:
        baseline = load_results(args.baseline)
        gate_result = gate(document["metrics"], baseline["metrics"], args.max_drop)
        document["gate"] = {"baseline": args.baseline, "max_drop": args.max_drop, **asdict(gate_result)}
        verdict_ok = gate_result.passed
    paths = write_outputs(document, args.out)
    if args.json:
        print(json.dumps(document, indent=2))
    else:
        print(render_summary(document))
        if "gate" in document:
            print(f"gate against {args.baseline}: {'PASS' if document['gate']['passed'] else 'FAIL'}")
            for reason in document["gate"]["reasons"]:
                print(f"  {reason}")
        print(f"wrote {paths['json']} and {paths['markdown']}")
    return 0 if verdict_ok else 1


def cmd_gate(args: argparse.Namespace) -> int:
    current, baseline = load_results(args.results), load_results(args.baseline)
    result = gate(current["metrics"], baseline["metrics"], args.max_drop)
    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print(f"gate: {'PASS' if result.passed else 'FAIL'} (current {current['metrics']['overall']['pass_rate']:.2f} vs baseline {baseline['metrics']['overall']['pass_rate']:.2f}, max drop {args.max_drop:.2f})")
        for reason in result.reasons:
            print(f"  {reason}")
        for note in result.notes:
            print(f"  note: {note}")
    return 0 if result.passed else 1


def cmd_fake_kubectl(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.scenario)
    if args.out:
        path = write_fake_kubectl(scenario, args.out)
        print(path)
    else:
        sys.stdout.write(render_fake_kubectl(scenario.kubectl))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    document = load_results(args.results)
    sys.stdout.write(render_markdown(document))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agenteval", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run every scenario N times, grade, write results.json and results.md")
    run.add_argument("--scenarios", action="append", required=True, metavar="PATH", help="a scenario file or a directory of them (repeat)")
    run.add_argument("--trials", type=int, default=3)
    run.add_argument("--replay", action="store_true", help="use each scenario's scripted model responses instead of the API")
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    run.add_argument("--judge", action="store_true", help="add the LLM-judge grader for hypothesis quality (calls the API)")
    run.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    run.add_argument("--out", default="results", metavar="DIR")
    run.add_argument("--baseline", metavar="results.json", help="gate this run against a saved results file")
    run.add_argument("--max-drop", type=float, default=0.1, help="largest pass-rate drop the gate tolerates")
    run.add_argument("--min-pass-rate", type=float, default=1.0, help="exit 1 below this overall pass rate when there is no baseline")
    run.add_argument("--triage-agent", default=str(DEFAULT_TRIAGE_AGENT))
    run.add_argument("--mcp-server", default=str(DEFAULT_MCP_SERVER))
    run.add_argument("--json", action="store_true")

    gate_cmd = sub.add_parser("gate", help="compare a results file with a baseline")
    gate_cmd.add_argument("results")
    gate_cmd.add_argument("--baseline", required=True)
    gate_cmd.add_argument("--max-drop", type=float, default=0.1)
    gate_cmd.add_argument("--json", action="store_true")

    fake = sub.add_parser("fake-kubectl", help="render the fake kubectl a scenario produces")
    fake.add_argument("scenario")
    fake.add_argument("--out", metavar="DIR", help="write it as DIR/kubectl instead of printing")

    show = sub.add_parser("show", help="print a results file as Markdown")
    show.add_argument("results")
    return parser


def main(argv: Optional[Sequence[str]] = None, agent_factory: Optional[Callable[[argparse.Namespace], Callable[[Scenario, int], Trial]]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "run":
            return cmd_run(args, agent_factory)
        if args.command == "gate":
            return cmd_gate(args)
        if args.command == "fake-kubectl":
            return cmd_fake_kubectl(args)
        return cmd_show(args)
    except (ScenarioError, MissingLesson) as exc:
        print(f"agenteval: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"agenteval: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
