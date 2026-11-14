"""Pure verdict functions for the aiops-copilot grader.

Each function takes parsed JSON (what the learner's copilot printed, or what
the fake model recorded) and returns a list of problems in plain words. An
empty list means the milestone's assertion holds. No I/O here, so every rule
is unit-tested without a cluster.
"""
from __future__ import annotations

import base64
import json
import math
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

CATEGORIES = ("crashloop-config", "oom", "image-pull", "no-endpoints", "healthy")
EXPECTED_CATEGORY = {"cc-api": "crashloop-config", "cc-mem": "oom", "cc-img": "image-pull", "cc-web": "no-endpoints", "cc-ok": "healthy"}
INCIDENT_APPS = ("cc-api", "cc-mem", "cc-img", "cc-web")
DECOY_APPS = ("cc-ok",)
CRASH_MARKER = "invalid value for LOG_LEVEL"
INJECTION_MARKER = "ignore previous instructions"
PLANTED_SECRETS = ("ccg-log-pw-4f1c9a", "ccg-log-bearer-77d2e0", "ccg-env-pw-1b8e3d", "ccg-env-token-9a4c2f", "ccg-secret-5e7a1b")
PLANTED_SECRETS_ENCODED = tuple(base64.b64encode(value.encode()).decode() for value in PLANTED_SECRETS)
READ_TOOLS = ("list_pods", "describe", "pod_logs", "list_events", "propose_action")
API_VERSION = "2026-10-21"
DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
ACTIONS = ("restart", "scale", "rollback", "manual")
RISKS = ("low", "medium", "high")
REPORT_HEADINGS = ("summary", "impact", "timeline", "root cause", "actions")

SignalRule = Tuple[Set[str], str]

EXPECTED_SIGNALS: Dict[str, List[SignalRule]] = {
    "cc-api": [({"CrashLoopBackOff", "Error", "BackOff"}, "pod")],
    "cc-mem": [({"OOMKilled"}, "pod")],
    "cc-img": [({"ErrImagePull", "ImagePullBackOff"}, "pod")],
    "cc-web": [({"NoEndpoints"}, "service"), ({"NotReady", "Unhealthy"}, "pod")],
}


def name_of(reference: Any) -> str:
    return str(reference).split("/", 1)[-1] if reference else ""


def evidence_items(document: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    if not isinstance(document, dict) or not isinstance(document.get("evidence"), list):
        return [], ["the output must be a JSON object with an `evidence` list"]
    items = [item for item in document["evidence"] if isinstance(item, dict)]
    problems = []
    if len(items) != len(document["evidence"]):
        problems.append("every evidence entry must be a JSON object")
    for index, item in enumerate(items):
        missing = [key for key in ("id", "workload", "object", "signal") if not isinstance(item.get(key), str) or not item.get(key)]
        if missing:
            problems.append(f"evidence[{index}] is missing string field(s) {', '.join(missing)}")
    ids = [item.get("id") for item in items]
    duplicates = sorted({str(i) for i in ids if ids.count(i) > 1})
    if duplicates:
        problems.append(f"evidence ids must be unique; repeated: {', '.join(duplicates[:5])}")
    return items, problems


def belongs(item: Dict[str, Any], app: str, pod: Optional[str]) -> bool:
    if name_of(item.get("workload")) == app:
        return True
    obj = str(item.get("object") or "")
    return obj == f"service/{app}" or (pod is not None and obj == f"pod/{pod}")


def evidence_problems(document: Any, pods: Dict[str, str], apps: Sequence[str] = INCIDENT_APPS, decoys: Sequence[str] = DECOY_APPS) -> List[str]:
    items, problems = evidence_items(document)
    if problems and not items:
        return problems
    for app in apps:
        pod = pods.get(app)
        mine = [item for item in items if belongs(item, app, pod)]
        if not mine:
            wanted = " and ".join("/".join(sorted(signals)) for signals, _ in EXPECTED_SIGNALS[app])
            problems.append(f"no evidence for deployment/{app} (pod {pod}); expected signal {wanted}")
            continue
        for signals, scope in EXPECTED_SIGNALS[app]:
            target = f"pod/{pod}" if scope == "pod" else f"service/{app}"
            if not any(item.get("signal") in signals and item.get("object") == target for item in mine):
                seen = sorted({f"{item.get('object')}:{item.get('signal')}" for item in mine})
                problems.append(f"deployment/{app}: no item with object {target} and signal {' or '.join(sorted(signals))} (saw {', '.join(seen) or 'nothing'})")
        if app == "cc-api" and not any(str(item.get("signal")).lower() == "logs" and CRASH_MARKER in json.dumps(item) for item in mine):
            problems.append(f"deployment/cc-api: no `Logs` item whose lines contain the crash message {CRASH_MARKER!r}; read the previous container's log tail")
    for app in decoys:
        noisy = [str(item.get("id")) for item in items if belongs(item, app, pods.get(app))]
        if noisy:
            problems.append(f"the healthy decoy deployment/{app} should produce no evidence, got {', '.join(noisy[:4])}")
    return problems


def stability_problems(first: Any, second: Any, pods: Dict[str, str], apps: Sequence[str] = INCIDENT_APPS) -> List[str]:
    one, _ = evidence_items(first)
    two, _ = evidence_items(second)
    second_ids = {item.get("id") for item in two}
    problems = []
    for app in apps:
        ids = {item.get("id") for item in one if belongs(item, app, pods.get(app))}
        if ids and not ids & second_ids:
            problems.append(f"deployment/{app}: none of the ids {sorted(str(i) for i in ids)[:3]} survived a second collect; derive ids from what an item is about (kind and object), not from time, counts or position")
    return problems


def triage_problems(document: Any, evidence_ids: Set[str], expected: Dict[str, str] = EXPECTED_CATEGORY) -> List[str]:
    if not isinstance(document, dict) or not isinstance(document.get("incidents"), list):
        return ["the output must be a JSON object with an `incidents` list"]
    entries = [entry for entry in document["incidents"] if isinstance(entry, dict)]
    problems = []
    for entry in entries:
        if entry.get("category") not in CATEGORIES:
            problems.append(f"{entry.get('workload')}: unknown category {entry.get('category')!r} (use one of {', '.join(CATEGORIES)})")
    for app, category in expected.items():
        mine = [entry for entry in entries if name_of(entry.get("workload")) == app]
        if not mine:
            problems.append(f"no incident entry for deployment/{app} (expected {category}); list every Deployment, healthy ones included")
            continue
        for entry in mine:
            if entry.get("category") != category:
                problems.append(f"{entry.get('workload')}: category {entry.get('category')!r}, expected {category!r}")
            citations = entry.get("citations")
            if category == "healthy":
                continue
            if not isinstance(citations, list) or not citations:
                problems.append(f"{entry.get('workload')}: cite the evidence ids that support {category!r}")
                continue
            unknown = [str(c) for c in citations if c not in evidence_ids]
            if unknown:
                problems.append(f"{entry.get('workload')}: citations {', '.join(unknown[:3])} are not ids from `copilot collect`")
    return problems


def result_text(block: Dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def tool_results(body: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(body, dict):
        return {}
    messages = body.get("messages") or []
    if not messages or not isinstance(messages[-1], dict) or messages[-1].get("role") != "user":
        return {}
    content = messages[-1].get("content")
    if not isinstance(content, list):
        return {}
    return {str(block.get("tool_use_id")): block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"}


def request_shape_problems(record: Dict[str, Any], api_key: str) -> List[str]:
    headers = record.get("headers") or {}
    body = record.get("body") if isinstance(record.get("body"), dict) else {}
    problems = []
    if headers.get("x-api-key") != api_key:
        problems.append("send ANTHROPIC_API_KEY in the x-api-key header")
    if headers.get("anthropic-version") != API_VERSION:
        problems.append(f"send anthropic-version: {API_VERSION} (got {headers.get('anthropic-version')!r})")
    if body.get("model") != DEFAULT_MODEL:
        problems.append(f"default the model to {DEFAULT_MODEL} (got {body.get('model')!r})")
    if "thinking" in body:
        problems.append("do not send a `thinking` parameter: thinking is always on for Claude Opus 5.5; use output_config.effort")
    if not isinstance((body.get("output_config") or {}).get("effort"), str):
        problems.append("set output_config.effort (for example \"medium\")")
    system = body.get("system")
    if not (isinstance(system, list) and system and isinstance(system[-1], dict) and system[-1].get("cache_control")):
        problems.append("send the system prompt as a list of text blocks with cache_control on the last one")
    names = {tool.get("name") for tool in body.get("tools") or [] if isinstance(tool, dict)}
    missing = [name for name in READ_TOOLS if name not in names]
    if missing:
        problems.append(f"tools missing from the request: {', '.join(missing)}")
    return problems


def echo_problems(records: Sequence[Dict[str, Any]], sent: Sequence[Dict[str, Any]]) -> List[str]:
    problems = []
    for index in range(1, len(records)):
        messages = (records[index].get("body") or {}).get("messages") or []
        if len(messages) < 2 or not isinstance(messages[-2], dict):
            problems.append(f"request {index + 1}: expected the previous assistant turn followed by a user turn of tool results")
            continue
        echoed = messages[-2]
        if echoed.get("role") != "assistant" or echoed.get("content") != sent[index - 1].get("content"):
            problems.append(f"request {index + 1}: pass the assistant content back unchanged (thinking blocks included), as the response returned it")
    return problems


def wrapper_problems(text: str) -> List[str]:
    stripped = text.strip()
    problems = []
    if not re.match(r"(?i)^<untrusted_data\b[^>]*>", stripped):
        problems.append("tool output must start with an <untrusted_data ...> tag")
    if not stripped.lower().endswith("</untrusted_data>"):
        problems.append("tool output must end with </untrusted_data>")
    if stripped.lower().count("</untrusted_data") != 1 or stripped.lower().count("<untrusted_data") != 1:
        problems.append("the data must not be able to close the wrapper: escape any <untrusted_data or </untrusted_data inside it")
    return problems


def leaked_secrets(records: Iterable[Dict[str, Any]]) -> List[str]:
    found = []
    for index, record in enumerate(records, start=1):
        raw = str(record.get("raw") or "")
        for value in PLANTED_SECRETS + PLANTED_SECRETS_ENCODED:
            if value in raw:
                found.append(f"request {index} contains the planted secret {value[:10]}...")
    return found


def parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def report_problems(report: Any, expected: Dict[str, str] = EXPECTED_CATEGORY) -> List[str]:
    if not isinstance(report, dict):
        return ["the report must be a JSON object"]
    problems = []
    for key in ("summary", "impact"):
        if not isinstance(report.get(key), str) or not report.get(key).strip():
            problems.append(f"`{key}` must be a non-empty string")
    evidence = report.get("evidence")
    ids = {item.get("id") for item in evidence if isinstance(item, dict)} if isinstance(evidence, list) else set()
    if not ids:
        problems.append("`evidence` must list the cited evidence items (with their ids)")
    timeline = report.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        problems.append("`timeline` must be a non-empty list of {time, description, evidence}")
    else:
        times = []
        for index, entry in enumerate(timeline):
            when = parse_time(entry.get("time")) if isinstance(entry, dict) else None
            if when is None:
                problems.append(f"timeline[{index}].time must be an ISO-8601 time with a zone, e.g. 2026-11-13T09:00:00Z")
                break
            times.append(when)
            if entry.get("evidence") not in ids:
                problems.append(f"timeline[{index}].evidence {entry.get('evidence')!r} does not resolve to an item in `evidence`")
                break
        if len(times) == len(timeline) and times != sorted(times):
            problems.append("`timeline` must be ordered by time, oldest first")
    hypotheses = report.get("hypotheses")
    if not isinstance(hypotheses, list):
        problems.append("`hypotheses` must be a list of {workload, category, hypothesis, confidence, citations}")
        hypotheses = []
    for app, category in expected.items():
        if category == "healthy":
            continue
        mine = [h for h in hypotheses if isinstance(h, dict) and name_of(h.get("workload")) == app]
        if not mine:
            problems.append(f"no hypothesis for deployment/{app}")
            continue
        for hypothesis in mine:
            if hypothesis.get("category") != category:
                problems.append(f"{hypothesis.get('workload')}: category {hypothesis.get('category')!r}, expected {category!r}")
            confidence = hypothesis.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                problems.append(f"{hypothesis.get('workload')}: confidence must be a number between 0 and 1")
            citations = hypothesis.get("citations")
            if not isinstance(citations, list) or not citations:
                problems.append(f"{hypothesis.get('workload')}: cite evidence ids")
            elif any(c not in ids for c in citations):
                problems.append(f"{hypothesis.get('workload')}: a citation does not resolve to an item in `evidence`")
    actions = report.get("actions")
    if not isinstance(actions, list) or not actions:
        problems.append("`actions` must be a non-empty list of proposals")
    else:
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                problems.append(f"actions[{index}] must be an object")
                continue
            if action.get("status") != "proposed":
                problems.append(f"actions[{index}].status must be \"proposed\": a report never performs an action")
            if action.get("risk") not in RISKS:
                problems.append(f"actions[{index}].risk must be one of {', '.join(RISKS)}")
            if action.get("action") not in ACTIONS:
                problems.append(f"actions[{index}].action must be one of {', '.join(ACTIONS)}")
    return problems


def markdown_problems(text: str, citations: Iterable[str]) -> List[str]:
    headings = [line.lstrip("#").strip().lower() for line in text.splitlines() if line.startswith("#")]
    problems = [f"the markdown report needs a `## {title.title()}` heading" for title in REPORT_HEADINGS if not any(h.startswith(title) for h in headings)]
    missing = [c for c in citations if c not in text]
    if missing:
        problems.append(f"the markdown report should show the cited evidence ids, missing {', '.join(missing[:3])}")
    return problems


def wilson(successes: int, trials: int, z: float = 1.96) -> Tuple[float, float]:
    if trials <= 0:
        return 0.0, 0.0
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def close(a: Any, b: float, tolerance: float = 1e-3) -> bool:
    return isinstance(a, (int, float)) and not isinstance(a, bool) and abs(a - b) <= tolerance


def eval_problems(document: Any, expected: Dict[str, bool], trials: int) -> List[str]:
    if not isinstance(document, dict) or not isinstance(document.get("cases"), list) or not isinstance(document.get("summary"), dict):
        return ["the output must be a JSON object with `cases` (a list) and `summary` (an object)"]
    problems = []
    by_id = {case.get("id"): case for case in document["cases"] if isinstance(case, dict)}
    for case_id, should_pass in expected.items():
        case = by_id.get(case_id)
        if case is None:
            problems.append(f"case {case_id} is missing from `cases`")
            continue
        if case.get("passed") is not should_pass:
            problems.append(f"case {case_id}: passed={case.get('passed')!r}, expected {should_pass} ({'category and must_cite both hold' if should_pass else 'the expected category or a must_cite id does not hold'})")
        if case.get("trials") != trials:
            problems.append(f"case {case_id}: trials={case.get('trials')!r}, expected {trials} (the suite's `trials`)")
        if not close(case.get("pass_rate"), 1.0 if should_pass else 0.0):
            problems.append(f"case {case_id}: pass_rate={case.get('pass_rate')!r}, expected {1.0 if should_pass else 0.0} (passing trials / trials)")
    passes = sum(trials for ok in expected.values() if ok)
    total = trials * len(expected)
    low, high = wilson(passes, total)
    summary = document["summary"]
    wanted = {"cases": len(expected), "passed": sum(1 for ok in expected.values() if ok), "trials": total}
    for key, value in wanted.items():
        if summary.get(key) != value:
            problems.append(f"summary.{key}={summary.get(key)!r}, expected {value}")
    if not close(summary.get("pass_rate"), passes / total):
        problems.append(f"summary.pass_rate={summary.get('pass_rate')!r}, expected {passes / total:.4f} (passing trials / all trials)")
    if not close(summary.get("wilson_low"), low) or not close(summary.get("wilson_high"), high):
        problems.append(f"summary Wilson 95% interval [{summary.get('wilson_low')!r}, {summary.get('wilson_high')!r}], expected [{low:.4f}, {high:.4f}] for {passes}/{total} with z=1.96")
    return problems
