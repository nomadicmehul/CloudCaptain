"""agenttrace: turn an agent run (one trace line per model call, one audit entry per tool call) into OpenTelemetry spans named with the GenAI semantic conventions, export them as OTLP/HTTP JSON, and read latency, tokens and cost back out of them."""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

SEMCONV_STATUS = "Development"
OPERATION_NAME = "gen_ai.operation.name"
PROVIDER_NAME = "gen_ai.provider.name"
AGENT_NAME = "gen_ai.agent.name"
REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
FINISH_REASONS = "gen_ai.response.finish_reasons"
TOOL_NAME = "gen_ai.tool.name"
ERROR_TYPE = "error.type"
STEP = "triage.step"
STEPS = "triage.steps"
TOOL_CALLS = "triage.tool_calls"
OUTCOME = "triage.outcome"
STOP = "triage.stop"
COST_USD = "triage.cost_usd"
ESTIMATED = "triage.duration_estimated"
VIOLATIONS = "triage.violations"

SPAN_KIND_INTERNAL = 1
SPAN_KIND_CLIENT = 3
STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2
DEFAULT_ENDPOINT = "http://localhost:4318/v1/traces"
RAN_OUTCOMES = ("ok", "error", "executed")
FAILED_OUTCOMES = ("error", "refused", "budget")
PRICES_PER_MTOK = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01},
}
HEX_ID = re.compile(r"^[0-9a-f]+$")

Transport = Callable[[str, Dict[str, str], bytes], Tuple[int, bytes]]


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    name: str
    kind: int
    start_ns: int
    end_ns: int
    attributes: Dict[str, Any] = field(default_factory=dict)
    status_code: int = STATUS_UNSET
    status_message: str = ""

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000_000


@dataclass
class RunSummary:
    trace_id: str
    models: List[str]
    steps: int
    tool_calls: int
    tool_requests: int
    tool_errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: float
    stop: str
    has_report: bool
    refused: bool


class IdGenerator:
    def __init__(self, seed: Optional[int] = None) -> None:
        self.rng: Any = random.Random(seed) if seed is not None else random.SystemRandom()

    def trace_id(self) -> str:
        return self._hex(16)

    def span_id(self) -> str:
        return self._hex(8)

    def _hex(self, nbytes: int) -> str:
        while True:
            value = self.rng.getrandbits(nbytes * 8)
            if value:
                return format(value, f"0{nbytes * 2}x")


def estimate_cost(model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0) -> float:
    prices = PRICES_PER_MTOK.get(model)
    if not prices:
        return 0.0
    return round((input_tokens * prices["input"] + output_tokens * prices["output"] + cache_read_tokens * prices["cache_read"]) / 1_000_000, 6)


def parse_trace_lines(text: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"trace line {number}: not JSON ({exc.msg})") from None
        if not isinstance(event, dict) or "step" not in event:
            raise ValueError(f"trace line {number}: expected an object with a \"step\" field")
        events.append(event)
    return sorted(events, key=lambda event: int(event["step"]))


def parse_start(value: Optional[str], elapsed_seconds: float = 0.0, now_ns: Callable[[], int] = time.time_ns) -> int:
    if value is None:
        return now_ns() - int(elapsed_seconds * 1_000_000_000)
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"--start {value!r} is not ISO 8601 (try 2026-11-01T02:03:00Z)") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp()) * 1_000_000_000 + moment.microsecond * 1000


def build_spans(trace: Sequence[Dict[str, Any]], outcome: Optional[Dict[str, Any]], start_ns: int, ids: IdGenerator, agent: str = "triage_agent", provider: str = "anthropic") -> List[Span]:
    outcome = outcome or {}
    audit = [entry for entry in (outcome.get("audit") or []) if isinstance(entry, dict)]
    model_ms = sum(int(event.get("elapsed_ms") or 0) for event in trace)
    total_ms = float(outcome["elapsed_seconds"]) * 1000 if outcome.get("elapsed_seconds") is not None else float(model_ms)
    unmeasured = [entry for entry in audit if entry.get("outcome") in RAN_OUTCOMES and entry.get("elapsed_ms") is None]
    share_ms = max(0.0, total_ms - model_ms) / len(unmeasured) if unmeasured else 0.0
    by_step: Dict[int, List[Dict[str, Any]]] = {}
    for entry in audit:
        by_step.setdefault(int(entry.get("step") or 0), []).append(entry)

    trace_id = ids.trace_id()
    root = Span(trace_id, ids.span_id(), None, f"invoke_agent {agent}", SPAN_KIND_INTERNAL, start_ns, start_ns, {OPERATION_NAME: "invoke_agent", AGENT_NAME: agent, PROVIDER_NAME: provider})
    spans = [root]
    cursor = start_ns
    input_total = output_total = 0
    cost_total = 0.0
    for event in trace:
        step = int(event["step"])
        model = str(event.get(REQUEST_MODEL) or outcome.get("model") or "unknown")
        input_tokens = int(event.get(INPUT_TOKENS) or 0)
        output_tokens = int(event.get(OUTPUT_TOKENS) or 0)
        cost = estimate_cost(model, input_tokens, output_tokens)
        chat = Span(trace_id, ids.span_id(), root.span_id, f"chat {model}", SPAN_KIND_CLIENT, cursor, cursor + int(event.get("elapsed_ms") or 0) * 1_000_000, {
            OPERATION_NAME: "chat",
            PROVIDER_NAME: provider,
            REQUEST_MODEL: model,
            RESPONSE_MODEL: str(event.get(RESPONSE_MODEL) or model),
            INPUT_TOKENS: input_tokens,
            OUTPUT_TOKENS: output_tokens,
            FINISH_REASONS: [str(reason) for reason in (event.get(FINISH_REASONS) or [])],
            STEP: step,
            COST_USD: cost,
        })
        spans.append(chat)
        cursor = chat.end_ns
        for entry in by_step.get(step, []):
            spans.append(tool_span(entry, chat, cursor, ids, share_ms))
            cursor = spans[-1].end_ns
        input_total += input_tokens
        output_total += output_tokens
        cost_total += cost

    root.end_ns = max(cursor, start_ns + int(round(total_ms * 1_000_000)))
    root.attributes.update({INPUT_TOKENS: input_total, OUTPUT_TOKENS: output_total, STEPS: len(trace), TOOL_CALLS: sum(1 for entry in audit if entry.get("outcome") in RAN_OUTCOMES), COST_USD: round(float(outcome.get("cost_usd") or cost_total), 6)})
    if outcome:
        stop = str(outcome.get("stop") or "")
        root.attributes[STOP] = stop
        root.attributes[VIOLATIONS] = len(outcome.get("violations") or [])
        if stop == "report":
            root.status_code = STATUS_OK
        elif stop and stop != "end_turn":
            root.status_code, root.status_message = STATUS_ERROR, f"agent stopped with {stop} and wrote no report"
    return spans


def tool_span(entry: Dict[str, Any], parent: Span, start_ns: int, ids: IdGenerator, share_ms: float) -> Span:
    outcome = str(entry.get("outcome") or "")
    measured = entry.get("elapsed_ms")
    estimated = measured is None and outcome in RAN_OUTCOMES
    duration_ms = share_ms if estimated else float(measured or 0)
    span = Span(parent.trace_id, ids.span_id(), parent.span_id, f"execute_tool {entry.get('tool')}", SPAN_KIND_INTERNAL, start_ns, start_ns + int(round(duration_ms * 1_000_000)), {
        OPERATION_NAME: "execute_tool",
        TOOL_NAME: str(entry.get("tool")),
        STEP: int(entry.get("step") or 0),
        OUTCOME: outcome,
        ESTIMATED: estimated,
    })
    if outcome in FAILED_OUTCOMES:
        span.attributes[ERROR_TYPE] = outcome
        span.status_code, span.status_message = STATUS_ERROR, str(entry.get("detail") or "")[:120]
    return span


def encode_value(value: Any) -> Dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [encode_value(item) for item in value]}}
    return {"stringValue": str(value)}


def decode_value(value: Dict[str, Any]) -> Any:
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "arrayValue" in value:
        return [decode_value(item) for item in value["arrayValue"].get("values", [])]
    return None


def encode_attributes(attributes: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"key": key, "value": encode_value(value)} for key, value in attributes.items()]


def decode_attributes(items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    return {item["key"]: decode_value(item.get("value") or {}) for item in items if "key" in item}


def encode_span(span: Span) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "traceId": span.trace_id,
        "spanId": span.span_id,
        "name": span.name,
        "kind": span.kind,
        "startTimeUnixNano": str(span.start_ns),
        "endTimeUnixNano": str(span.end_ns),
        "attributes": encode_attributes(span.attributes),
        "status": {"code": span.status_code},
    }
    if span.parent_span_id:
        data["parentSpanId"] = span.parent_span_id
    if span.status_message:
        data["status"]["message"] = span.status_message
    return data


def to_otlp(spans: Sequence[Span], service: str = "triage-agent", scope_name: str = "agenttrace", scope_version: str = "1.0.0") -> Dict[str, Any]:
    return {"resourceSpans": [{"resource": {"attributes": encode_attributes({"service.name": service})}, "scopeSpans": [{"scope": {"name": scope_name, "version": scope_version}, "spans": [encode_span(span) for span in spans]}]}]}


def from_otlp(payload: Dict[str, Any]) -> List[Span]:
    spans: List[Span] = []
    for resource in payload.get("resourceSpans") or []:
        for scope in resource.get("scopeSpans") or []:
            for item in scope.get("spans") or []:
                status = item.get("status") or {}
                spans.append(Span(item["traceId"], item["spanId"], item.get("parentSpanId") or None, item.get("name", ""), int(item.get("kind") or 0), int(item["startTimeUnixNano"]), int(item["endTimeUnixNano"]), decode_attributes(item.get("attributes") or []), int(status.get("code") or 0), str(status.get("message") or "")))
    return spans


def service_of(payload: Dict[str, Any]) -> str:
    for resource in payload.get("resourceSpans") or []:
        attributes = decode_attributes((resource.get("resource") or {}).get("attributes") or [])
        if attributes.get("service.name"):
            return str(attributes["service.name"])
    return "unknown_service"


def validate_otlp(payload: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    resources = payload.get("resourceSpans")
    if not isinstance(resources, list) or not resources:
        return ["payload needs a non-empty resourceSpans list"]
    for resource in resources:
        for scope in resource.get("scopeSpans") or []:
            for item in scope.get("spans") or []:
                name = item.get("name", "?")
                if not (isinstance(item.get("traceId"), str) and len(item["traceId"]) == 32 and HEX_ID.match(item["traceId"])):
                    problems.append(f"{name}: traceId must be 32 lowercase hex characters")
                if not (isinstance(item.get("spanId"), str) and len(item["spanId"]) == 16 and HEX_ID.match(item["spanId"])):
                    problems.append(f"{name}: spanId must be 16 lowercase hex characters")
                parent = item.get("parentSpanId")
                if parent is not None and not (isinstance(parent, str) and len(parent) == 16 and HEX_ID.match(parent)):
                    problems.append(f"{name}: parentSpanId must be 16 lowercase hex characters")
                for key in ("startTimeUnixNano", "endTimeUnixNano"):
                    if not (isinstance(item.get(key), str) and item[key].isdigit()):
                        problems.append(f"{name}: {key} must be a string of digits (int64 is a string in protobuf JSON)")
                if all(isinstance(item.get(key), str) and item[key].isdigit() for key in ("startTimeUnixNano", "endTimeUnixNano")) and int(item["endTimeUnixNano"]) < int(item["startTimeUnixNano"]):
                    problems.append(f"{name}: ends before it starts")
                for attribute in item.get("attributes") or []:
                    value = attribute.get("value") or {}
                    typed = [key for key in ("stringValue", "intValue", "doubleValue", "boolValue", "arrayValue") if key in value]
                    if len(typed) != 1:
                        problems.append(f"{name}: attribute {attribute.get('key')!r} needs exactly one typed value, got {typed or 'none'}")
                    elif typed == ["intValue"] and not isinstance(value["intValue"], str):
                        problems.append(f"{name}: attribute {attribute.get('key')!r} intValue must be a string")
    return problems


def short_id(value: str) -> str:
    return value[:8]


def format_ms(span: Span) -> str:
    prefix = "~" if span.attributes.get(ESTIMATED) else ""
    return f"{prefix}{span.duration_ms:.0f} ms"


def describe(span: Span) -> str:
    attributes = span.attributes
    operation = attributes.get(OPERATION_NAME)
    if operation == "chat":
        reasons = ",".join(attributes.get(FINISH_REASONS) or []) or "-"
        return f"step {attributes.get(STEP, '?')}  {attributes.get(INPUT_TOKENS, 0)} in / {attributes.get(OUTPUT_TOKENS, 0)} out  ${float(attributes.get(COST_USD) or 0):.4f}  {reasons}"
    if operation == "execute_tool":
        detail = str(attributes.get(OUTCOME) or "")
        if span.status_code == STATUS_ERROR and span.status_message:
            detail += f"  {span.status_message[:60]}"
        return detail
    if operation == "invoke_agent":
        return f"{attributes.get(STEPS, 0)} steps  {attributes.get(TOOL_CALLS, 0)} tool calls  {attributes.get(INPUT_TOKENS, 0)} in / {attributes.get(OUTPUT_TOKENS, 0)} out  ${float(attributes.get(COST_USD) or 0):.4f}  stop {attributes.get(STOP, '-')}  violations {attributes.get(VIOLATIONS, 0)}"
    return ""


def render_tree(spans: Sequence[Span], service: str = "") -> str:
    children: Dict[Optional[str], List[Span]] = {}
    for span in spans:
        children.setdefault(span.parent_span_id, []).append(span)
    for group in children.values():
        group.sort(key=lambda span: span.start_ns)
    known = {span.span_id for span in spans}
    roots = [span for span in spans if span.parent_span_id is None or span.parent_span_id not in known]
    lines: List[str] = []

    def walk(span: Span, prefix: str, last: bool) -> None:
        branch = "└─ " if last else "├─ "
        status = "  ERROR" if span.status_code == STATUS_ERROR else ""
        lines.append(f"{prefix}{branch}{span.name:<34} {format_ms(span):>10}   {describe(span)}{status}")
        own = children.get(span.span_id, [])
        for index, child in enumerate(own):
            walk(child, prefix + ("   " if last else "│  "), index == len(own) - 1)

    for root in sorted(roots, key=lambda span: span.start_ns):
        started = datetime.fromtimestamp(root.start_ns / 1_000_000_000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        lines.append(f"trace {short_id(root.trace_id)}  {service or 'unknown_service'}  {started}  {root.duration_ms / 1000:.1f} s")
        walk(root, "", True)
    return "\n".join(lines)


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return float(ordered[rank - 1])


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize_run(spans: Sequence[Span]) -> RunSummary:
    chats = [span for span in spans if span.attributes.get(OPERATION_NAME) == "chat"]
    tools = [span for span in spans if span.attributes.get(OPERATION_NAME) == "execute_tool"]
    roots = [span for span in spans if span.parent_span_id is None]
    root = roots[0] if roots else None
    models = sorted({str(span.attributes.get(REQUEST_MODEL)) for span in chats})
    input_tokens = sum(int(span.attributes.get(INPUT_TOKENS) or 0) for span in chats)
    output_tokens = sum(int(span.attributes.get(OUTPUT_TOKENS) or 0) for span in chats)
    cost = float(root.attributes.get(COST_USD)) if root and root.attributes.get(COST_USD) is not None else sum(float(span.attributes.get(COST_USD) or 0) for span in chats)
    stop = str(root.attributes.get(STOP) or "") if root else ""
    has_report = stop == "report" or any(span.attributes.get(OUTCOME) == "report" for span in tools)
    refused = stop == "refusal" or any("refusal" in (span.attributes.get(FINISH_REASONS) or []) for span in chats)
    duration = root.duration_ms if root else sum(span.duration_ms for span in spans)
    ran = [span for span in tools if span.attributes.get(OUTCOME) in RAN_OUTCOMES or span.attributes.get(OUTCOME) is None]
    requests = [span for span in tools if span.attributes.get(OUTCOME) != "report"]
    errors = sum(1 for span in requests if span.status_code == STATUS_ERROR)
    return RunSummary(spans[0].trace_id if spans else "", models, len(chats), len(ran), len(requests), errors, input_tokens, output_tokens, round(cost, 6), duration, stop, has_report, refused)


def group_runs(spans: Sequence[Span]) -> List[List[Span]]:
    groups: Dict[str, List[Span]] = {}
    for span in spans:
        groups.setdefault(span.trace_id, []).append(span)
    return list(groups.values())


def metrics(runs: Sequence[Sequence[Span]]) -> Dict[str, Any]:
    summaries = [summarize_run(run) for run in runs if run]
    latency: Dict[str, List[float]] = {}
    for run in runs:
        for span in run:
            if span.attributes.get(OPERATION_NAME) == "chat":
                latency.setdefault(str(span.attributes.get(REQUEST_MODEL)), []).append(span.duration_ms)
    tool_total = sum(summary.tool_requests for summary in summaries)
    tool_errors = sum(summary.tool_errors for summary in summaries)
    costs = [summary.cost_usd for summary in summaries]
    return {
        "runs": len(summaries),
        "latency_ms": {model: {"p50": round(percentile(values, 50), 1), "p95": round(percentile(values, 95), 1), "spans": len(values)} for model, values in sorted(latency.items())},
        "tokens_per_run": {"input_mean": round(mean([s.input_tokens for s in summaries]), 1), "output_mean": round(mean([s.output_tokens for s in summaries]), 1), "input_p95": percentile([s.input_tokens for s in summaries], 95)},
        "cost_per_run_usd": {"mean": round(mean(costs), 6), "p95": round(percentile(costs, 95), 6), "total": round(sum(costs), 6)},
        "tool_calls_per_run": {"mean": round(mean([s.tool_calls for s in summaries]), 2), "p95": percentile([s.tool_calls for s in summaries], 95)},
        "tool_error_rate": round(tool_errors / tool_total, 4) if tool_total else 0.0,
        "runs_without_report": sum(1 for s in summaries if not s.has_report),
        "refusal_rate": round(sum(1 for s in summaries if s.refused) / len(summaries), 4) if summaries else 0.0,
        "stops": dict(Counter(s.stop or "unknown" for s in summaries)),
    }


def render_metrics(report: Dict[str, Any]) -> str:
    lines = [f"runs: {report['runs']}   stops: {', '.join(f'{k}={v}' for k, v in sorted(report['stops'].items())) or '-'}"]
    for model, values in report["latency_ms"].items():
        lines.append(f"  chat latency {model:<20} p50 {values['p50']:>8.0f} ms   p95 {values['p95']:>8.0f} ms   ({values['spans']} spans)")
    tokens = report["tokens_per_run"]
    lines.append(f"  tokens per run        mean {tokens['input_mean']:.0f} in / {tokens['output_mean']:.0f} out   p95 {tokens['input_p95']:.0f} in")
    cost = report["cost_per_run_usd"]
    lines.append(f"  cost per run          mean ${cost['mean']:.4f}   p95 ${cost['p95']:.4f}   total ${cost['total']:.4f}")
    calls = report["tool_calls_per_run"]
    lines.append(f"  tool calls per run    mean {calls['mean']:.1f}   p95 {calls['p95']:.0f}")
    lines.append(f"  tool error rate       {report['tool_error_rate'] * 100:.1f}%")
    lines.append(f"  runs without report   {report['runs_without_report']}/{report['runs']}")
    lines.append(f"  refusal rate          {report['refusal_rate'] * 100:.1f}%")
    return "\n".join(lines)


def urllib_transport(url: str, headers: Dict[str, str], body: bytes) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach {url}: {exc.reason}. Is a collector listening? Start one with: docker run --rm -p 4318:4318 -v $PWD/collector.yaml:/etc/otelcol-contrib/config.yaml otel/opentelemetry-collector-contrib") from None


def export(payload: Dict[str, Any], endpoint: str = DEFAULT_ENDPOINT, transport: Transport = urllib_transport, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    problems = validate_otlp(payload)
    if problems:
        raise ValueError("payload is not valid OTLP JSON: " + "; ".join(problems[:3]))
    body = json.dumps(payload).encode("utf-8")
    status, raw = transport(endpoint, {"content-type": "application/json", **(headers or {})}, body)
    text = raw.decode("utf-8", "replace")
    if status != 200:
        raise RuntimeError(f"collector answered HTTP {status}: {text[:300] or '(empty body)'}")
    reply = json.loads(text) if text.strip() else {}
    partial = reply.get("partialSuccess") or {}
    return {"endpoint": endpoint, "spans": len(from_otlp(payload)), "bytes": len(body), "rejected_spans": int(partial.get("rejectedSpans") or 0), "message": str(partial.get("errorMessage") or "")}


def load_json(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_payloads(paths: Sequence[str]) -> List[Tuple[str, Dict[str, Any]]]:
    payloads: List[Tuple[str, Dict[str, Any]]] = []
    for path in paths:
        payload = load_json(path)
        problems = validate_otlp(payload)
        if problems:
            raise ValueError(f"{path}: {problems[0]}")
        payloads.append((path, payload))
    return payloads


def parse_headers(items: Sequence[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"--header {item!r} must look like Name=value")
        headers[key.strip()] = value.strip()
    return headers


def cmd_convert(args: argparse.Namespace) -> int:
    with open(args.trace, encoding="utf-8") as handle:
        trace = parse_trace_lines(handle.read())
    if not trace:
        raise ValueError(f"{args.trace}: no trace lines; run the agent with --trace FILE first")
    outcome = load_json(args.outcome) if args.outcome else None
    start_ns = parse_start(args.start, float((outcome or {}).get("elapsed_seconds") or 0))
    spans = build_spans(trace, outcome, start_ns, IdGenerator(args.seed), agent=args.agent)
    payload = to_otlp(spans, service=args.service)
    text = json.dumps(payload, indent=2 if args.out in (None, "-") else None)
    if args.out in (None, "-"):
        print(text)
    else:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"wrote {len(spans)} spans for trace {short_id(spans[0].trace_id)} to {args.out}", file=sys.stderr)
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    for _, payload in load_payloads(args.files):
        print(render_tree(from_otlp(payload), service_of(payload)))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    headers = parse_headers(args.header)
    for path, payload in load_payloads(args.files):
        result = export(payload, args.endpoint, headers=headers)
        note = f", {result['rejected_spans']} rejected: {result['message']}" if result["rejected_spans"] else ""
        print(f"{path}: {result['spans']} spans ({result['bytes']} bytes) accepted by {args.endpoint}{note}")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    runs: List[List[Span]] = []
    for _, payload in load_payloads(args.files):
        runs.extend(group_runs(from_otlp(payload)))
    report = metrics(runs)
    print(json.dumps(report, indent=2) if args.json else render_metrics(report))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agenttrace", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    convert = commands.add_parser("convert", help="agent trace JSONL (+ outcome JSON) -> OTLP/HTTP JSON")
    convert.add_argument("--trace", required=True, metavar="FILE", help="the agent's --trace output: one JSON line per model call")
    convert.add_argument("--outcome", metavar="FILE", help="the agent's --json output: audit entries become execute_tool spans")
    convert.add_argument("--start", metavar="ISO8601", help="when the run started (default: now minus elapsed_seconds)")
    convert.add_argument("--service", default="triage-agent", help="resource service.name")
    convert.add_argument("--agent", default="triage_agent", help="gen_ai.agent.name on the root span")
    convert.add_argument("--seed", type=int, help="deterministic trace and span ids (tests and fixtures)")
    convert.add_argument("-o", "--out", metavar="FILE", help="write the payload here instead of stdout")
    convert.set_defaults(func=cmd_convert)

    view = commands.add_parser("view", help="print an OTLP JSON file as a tree with durations, tokens and cost")
    view.add_argument("files", nargs="+")
    view.set_defaults(func=cmd_view)

    send = commands.add_parser("export", help="POST OTLP JSON files to a collector")
    send.add_argument("files", nargs="+")
    send.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    send.add_argument("--header", action="append", default=[], metavar="Name=value", help="extra HTTP header, e.g. Authorization=Basic ... (repeatable)")
    send.set_defaults(func=cmd_export)

    aggregate = commands.add_parser("metrics", help="latency, tokens, cost and error rates over many OTLP JSON files")
    aggregate.add_argument("files", nargs="+")
    aggregate.add_argument("--json", action="store_true")
    aggregate.set_defaults(func=cmd_metrics)

    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"agenttrace: {exc.filename}: no such file", file=sys.stderr)
        return 1
    except KeyError as exc:
        print(f"agenttrace: payload is missing the field {exc}; is this OTLP JSON from `convert`?", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"agenttrace: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
