"""minieval: run an eval suite against OpenAI-compatible model endpoints, score every case with deterministic assertions and a judge, report pass rates with Wilson intervals and pass@k, gate a candidate against a baseline with McNemar's exact test and budgets, check eval inputs for training-set contamination, and serve a scripted stub model."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

ASSERTION_TYPES = ("equals", "contains", "icontains", "not-contains", "regex", "json-schema", "numeric", "rubric")
DEFAULT_TEMPLATE = "{{input}}"
NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")
TEMPLATE_INPUT = re.compile(r"\{\{\s*input\s*\}\}")
FALLBACK_ANSWER = "I don't know."

JUDGE_TEMPLATE = """You grade one model output against a rubric. Grade only what the rubric asks for; a longer answer is not a better answer.

<Output>
{{output}}
</Output>
<Rubric>
{{rubric}}
</Rubric>

Answer with one JSON object and nothing else: {"reason": "<one sentence>", "pass": true or false, "score": <a number from 0.0 to 1.0>}"""


class SuiteError(ValueError):
    pass


class ProviderError(RuntimeError):
    pass


@dataclass
class Assertion:
    type: str
    value: Any
    tolerance: float = 0.0


@dataclass
class Case:
    id: str
    input: str
    assertions: List[Assertion]
    reference: Optional[str] = None
    critical: bool = False
    tags: List[str] = field(default_factory=list)


@dataclass
class GatePolicy:
    alpha: float = 0.05
    min_effect: float = 0.1
    max_p95_latency_ms: Optional[float] = None
    max_mean_cost_usd: Optional[float] = None
    require_improvement: bool = False


@dataclass
class Suite:
    name: str
    template: str
    cases: List[Case]
    gate: GatePolicy
    pricing: Dict[str, Dict[str, float]]
    digest: str
    path: str = ""


@dataclass
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float


@dataclass
class JudgeVerdict:
    passed: bool
    score: float
    reason: str


@dataclass
class AssertionResult:
    type: str
    passed: bool
    detail: str


@dataclass
class GateResult:
    passed: bool
    verdict: str
    reasons: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)


Judge = Callable[[str, str], JudgeVerdict]


def _require(cond: bool, where: str, message: str) -> None:
    if not cond:
        raise SuiteError(f"{where}: {message}")


def parse_assertion(raw: Any, where: str) -> Assertion:
    _require(isinstance(raw, dict), where, "an assertion is an object with 'type' and 'value'")
    kind = raw.get("type")
    _require(kind in ASSERTION_TYPES, where, f"unknown assertion type {kind!r}; use one of {', '.join(ASSERTION_TYPES)}")
    _require("value" in raw, where, f"{kind} needs a 'value'")
    value = raw["value"]
    if kind == "regex":
        try:
            re.compile(value)
        except (re.error, TypeError) as exc:
            raise SuiteError(f"{where}: bad regex {value!r}: {exc}") from exc
    if kind == "json-schema":
        _require(isinstance(value, dict), where, "json-schema needs a schema object as 'value'")
    if kind == "numeric":
        _require(isinstance(value, (int, float)) and not isinstance(value, bool), where, "numeric needs a number as 'value'")
    if kind in ("equals", "contains", "icontains", "not-contains", "rubric"):
        _require(isinstance(value, str) and value != "", where, f"{kind} needs a non-empty string as 'value'")
    tolerance = raw.get("tolerance", 0.0)
    _require(isinstance(tolerance, (int, float)) and tolerance >= 0, where, "tolerance must be a number >= 0")
    return Assertion(kind, value, float(tolerance))


def parse_suite(data: Dict[str, Any], path: str = "<suite>") -> Suite:
    _require(isinstance(data, dict), path, "a suite is a JSON object")
    name = data.get("name")
    _require(isinstance(name, str) and name != "", path, "'name' is required")
    template = data.get("template", DEFAULT_TEMPLATE)
    _require(isinstance(template, str) and TEMPLATE_INPUT.search(template) is not None, path, "'template' must contain {{input}}")
    raw_cases = data.get("cases")
    _require(isinstance(raw_cases, list) and len(raw_cases) > 0, path, "'cases' must be a non-empty list")
    cases: List[Case] = []
    seen: Set[str] = set()
    for index, raw in enumerate(raw_cases):
        where = f"{path}: cases[{index}]"
        _require(isinstance(raw, dict), where, "a case is an object")
        case_id = raw.get("id")
        _require(isinstance(case_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", case_id or "") is not None, where, "'id' must be a slug")
        where = f"{path}: case {case_id!r}"
        _require(case_id not in seen, where, "duplicate id")
        seen.add(case_id)
        _require(isinstance(raw.get("input"), str) and raw["input"].strip() != "", where, "'input' must be a non-empty string")
        raw_asserts = raw.get("assert")
        _require(isinstance(raw_asserts, list) and len(raw_asserts) > 0, where, "'assert' must be a non-empty list")
        assertions = [parse_assertion(item, f"{where} assert[{i}]") for i, item in enumerate(raw_asserts)]
        tags = raw.get("tags", [])
        _require(isinstance(tags, list) and all(isinstance(t, str) for t in tags), where, "'tags' must be a list of strings")
        cases.append(Case(case_id, raw["input"], assertions, raw.get("reference"), bool(raw.get("critical", False)), list(tags)))
    raw_gate = data.get("gate", {})
    _require(isinstance(raw_gate, dict), path, "'gate' must be an object")
    known = {f for f in GatePolicy.__dataclass_fields__}
    unknown = sorted(set(raw_gate) - known)
    _require(not unknown, path, f"unknown gate keys {unknown}; use {sorted(known)}")
    gate = GatePolicy(**raw_gate)
    pricing = data.get("pricing", {})
    _require(isinstance(pricing, dict), path, "'pricing' maps a model name to input_per_mtok and output_per_mtok")
    digest = hashlib.sha256(json.dumps({"template": template, "cases": raw_cases}, sort_keys=True).encode()).hexdigest()
    return Suite(name, template, cases, gate, pricing, digest, path)


def load_suite(path: str) -> Suite:
    try:
        data = json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise SuiteError(f"{path}: not valid JSON: {exc}") from exc
    return parse_suite(data, path)


def render_prompt(template: str, case: Case) -> str:
    return TEMPLATE_INPUT.sub(lambda _: case.input, template)


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "integer" if value.is_integer() else "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _type_ok(value: Any, wanted: Any) -> bool:
    if isinstance(wanted, list):
        return any(_type_ok(value, w) for w in wanted)
    actual = _json_type(value)
    return actual == wanted or (wanted == "number" and actual == "integer")


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def validate_schema(value: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    if "type" in schema and not _type_ok(value, schema["type"]):
        return [f"{path}: expected {schema['type']}, got {_json_type(value)}"]
    errors: List[str] = []
    if "const" in schema and not _same(value, schema["const"]):
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and not any(_same(value, option) for option in schema["enum"]):
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{path}: does not match {schema['pattern']!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required key {key!r}")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                errors.extend(validate_schema(item, properties[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected key {key!r}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                errors.extend(validate_schema(item, schema["items"], f"{path}[{index}]"))
    return errors


def first_number(text: str) -> Optional[float]:
    match = NUMBER_PATTERN.search(text.replace(",", ""))
    return float(match.group(0)) if match else None


def keyword_judge(output: str, rubric: str) -> JudgeVerdict:
    required = re.findall(r"'([^']+)'", rubric)
    if not required:
        return JudgeVerdict(False, 0.0, "rubric quotes no phrase, nothing to check")
    lowered = output.lower()
    missing = [phrase for phrase in required if phrase.lower() not in lowered]
    score = round((len(required) - len(missing)) / len(required), 4)
    if missing:
        return JudgeVerdict(False, score, "missing " + ", ".join(repr(p) for p in missing))
    return JudgeVerdict(True, score, f"all {len(required)} quoted phrase(s) present")


def render_judge_prompt(output: str, rubric: str) -> str:
    return JUDGE_TEMPLATE.replace("{{output}}", output).replace("{{rubric}}", rubric)


def split_judge_prompt(prompt: str) -> Optional[Tuple[str, str]]:
    output = re.search(r"<Output>\n?(.*?)\n?</Output>", prompt, re.S)
    rubric = re.search(r"<Rubric>\n?(.*?)\n?</Rubric>", prompt, re.S)
    if not output or not rubric:
        return None
    return output.group(1), rubric.group(1)


def parse_judge_text(text: str) -> JudgeVerdict:
    decoder = json.JSONDecoder()
    for start in [i for i, ch in enumerate(text) if ch == "{"]:
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("pass"), bool):
            score = obj.get("score", 1.0 if obj["pass"] else 0.0)
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                break
            return JudgeVerdict(obj["pass"], float(score), str(obj.get("reason", "")))
    return JudgeVerdict(False, 0.0, "judge did not return {reason, pass, score}")


def model_judge(provider: "OpenAIChatProvider") -> Judge:
    def judge(output: str, rubric: str) -> JudgeVerdict:
        completion = provider.complete(render_judge_prompt(output, rubric), seed=0, temperature=0.0)
        return parse_judge_text(completion.text)
    return judge


def check_assertion(assertion: Assertion, output: str, judge: Optional[Judge] = None) -> AssertionResult:
    kind, value = assertion.type, assertion.value
    if kind == "equals":
        return AssertionResult(kind, output == value, "exact match" if output == value else f"got {output[:60]!r}, want {value!r}")
    if kind == "contains":
        return AssertionResult(kind, value in output, f"{'found' if value in output else 'missing'} {value!r}")
    if kind == "icontains":
        found = value.lower() in output.lower()
        return AssertionResult(kind, found, f"{'found' if found else 'missing'} {value!r} (any case)")
    if kind == "not-contains":
        return AssertionResult(kind, value not in output, f"{'must not contain' if value in output else 'does not contain'} {value!r}")
    if kind == "regex":
        found = re.search(value, output) is not None
        return AssertionResult(kind, found, f"{'matches' if found else 'no match for'} /{value}/")
    if kind == "json-schema":
        try:
            document = json.loads(output)
        except json.JSONDecodeError as exc:
            return AssertionResult(kind, False, f"not JSON: {exc.msg} at char {exc.pos}")
        errors = validate_schema(document, value)
        return AssertionResult(kind, not errors, "; ".join(errors[:3]) if errors else "valid against the schema")
    if kind == "numeric":
        got = first_number(output)
        if got is None:
            return AssertionResult(kind, False, f"no number in output, want {value}")
        ok = abs(got - float(value)) <= assertion.tolerance + 1e-12
        return AssertionResult(kind, ok, f"got {got:g}, want {value:g} ± {assertion.tolerance:g}")
    if judge is None:
        return AssertionResult(kind, False, "no judge configured")
    verdict = judge(output, value)
    return AssertionResult(kind, verdict.passed, f"judge score {verdict.score:.2f}: {verdict.reason}")


class OpenAIChatProvider:
    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.clock = clock

    def request_body(self, prompt: str, seed: int, temperature: float, max_tokens: int) -> Dict[str, Any]:
        return {"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": temperature, "seed": seed, "max_tokens": max_tokens}

    def complete(self, prompt: str, seed: int = 0, temperature: float = 0.0, max_tokens: int = 512) -> Completion:
        body = json.dumps(self.request_body(prompt, seed, temperature, max_tokens)).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(f"{self.base_url}/chat/completions", data=body, headers=headers, method="POST")
        started = self.clock()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"{self.model}: HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ProviderError(f"{self.model}: cannot reach {self.base_url}: {exc}") from exc
        latency_ms = (self.clock() - started) * 1000.0
        try:
            text = payload["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.model}: response has no choices[0].message.content") from exc
        usage = payload.get("usage") or {}
        return Completion(text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), latency_ms)


def cost_usd(pricing: Dict[str, Dict[str, float]], model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price = pricing.get(model)
    if not price:
        return 0.0
    return (prompt_tokens * float(price.get("input_per_mtok", 0.0)) + completion_tokens * float(price.get("output_per_mtok", 0.0))) / 1_000_000


def pass_at_k(n: int, c: int, k: int) -> float:
    if k > n or n <= 0:
        raise ValueError(f"pass@k needs 1 <= k <= n, got n={n} k={k}")
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def wilson_interval(successes: int, trials: int, confidence: float = 0.95) -> Tuple[float, float]:
    if trials <= 0:
        return (0.0, 1.0)
    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    p = successes / trials
    denominator = 1.0 + z * z / trials
    center = (p + z * z / (2.0 * trials)) / denominator
    half = z * math.sqrt(p * (1.0 - p) / trials + z * z / (4.0 * trials * trials)) / denominator
    low = 0.0 if successes == 0 else max(0.0, center - half)
    high = 1.0 if successes == trials else min(1.0, center + half)
    return (low, high)


def mcnemar_exact(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1))
    return min(1.0, 2.0 * tail / 2 ** n)


def min_discordant_for(alpha: float) -> int:
    n = 1
    while mcnemar_exact(n, 0) >= alpha:
        n += 1
    return n


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def run_case(suite: Suite, case: Case, provider: OpenAIChatProvider, samples: int, temperature: float, judge: Optional[Judge]) -> Dict[str, Any]:
    prompt = render_prompt(suite.template, case)
    records = []
    for seed in range(samples):
        record: Dict[str, Any] = {"seed": seed}
        try:
            completion = provider.complete(prompt, seed=seed, temperature=temperature)
        except ProviderError as exc:
            record.update(output="", passed=False, error=str(exc), latency_ms=0.0, cost_usd=0.0, assertions=[])
            records.append(record)
            continue
        results = []
        error = None
        for assertion in case.assertions:
            try:
                results.append(check_assertion(assertion, completion.text, judge))
            except ProviderError as exc:
                error = f"judge: {exc}"
                results.append(AssertionResult(assertion.type, False, error))
        record.update(
            output=completion.text,
            passed=all(r.passed for r in results),
            latency_ms=round(completion.latency_ms, 3),
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            cost_usd=cost_usd(suite.pricing, provider.model, completion.prompt_tokens, completion.completion_tokens),
            assertions=[asdict(r) for r in results],
        )
        if error:
            record["error"] = error
        records.append(record)
    passes = sum(1 for r in records if r["passed"])
    ks = sorted({1, samples})
    return {
        "id": case.id,
        "critical": case.critical,
        "tags": case.tags,
        "samples": records,
        "passes": passes,
        "passed": passes == samples,
        "pass_at": {str(k): round(pass_at_k(samples, passes, k), 6) for k in ks},
    }


def summarize(cases: List[Dict[str, Any]], samples: int, confidence: float = 0.95) -> Dict[str, Any]:
    total = len(cases)
    passed = sum(1 for c in cases if c["passed"])
    low, high = wilson_interval(passed, total, confidence)
    calls = [s for c in cases for s in c["samples"] if "error" not in s]
    latencies = [s["latency_ms"] for s in calls]
    costs = [s["cost_usd"] for s in calls]
    ks = sorted({1, samples})
    return {
        "cases": total,
        "passed": passed,
        "pass_rate": round(passed / total, 6) if total else 0.0,
        "wilson_low": round(low, 6),
        "wilson_high": round(high, 6),
        "confidence": confidence,
        "pass_at": {str(k): round(sum(c["pass_at"][str(k)] for c in cases) / total, 6) for k in ks} if total else {},
        "errors": sum(1 for c in cases for s in c["samples"] if "error" in s),
        "latency_p50_ms": round(percentile(latencies, 50), 3),
        "latency_p95_ms": round(percentile(latencies, 95), 3),
        "mean_cost_usd": (sum(costs) / len(costs)) if costs else 0.0,
        "total_cost_usd": sum(costs),
    }


def run_suite(suite: Suite, provider: OpenAIChatProvider, samples: int = 1, temperature: float = 0.0, judge: Optional[Judge] = None, judge_name: str = "", progress: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    if samples < 1:
        raise SuiteError("--samples must be at least 1")
    cases = []
    for case in suite.cases:
        result = run_case(suite, case, provider, samples, temperature, judge)
        cases.append(result)
        if progress:
            progress(result)
    return {
        "suite": suite.name,
        "suite_sha256": suite.digest,
        "model": provider.model,
        "base_url": provider.base_url,
        "samples": samples,
        "temperature": temperature,
        "judge": judge_name or None,
        "gate": asdict(suite.gate),
        "summary": summarize(cases, samples),
        "cases": cases,
    }


def failure_detail(case: Dict[str, Any]) -> str:
    for sample in case["samples"]:
        if not sample["passed"]:
            if "error" in sample and not sample["assertions"]:
                return sample["error"]
            failed = [f"{a['type']}: {a['detail']}" for a in sample["assertions"] if not a["passed"]]
            return f"seed {sample['seed']}: " + "; ".join(failed)
    return ""


def gate(baseline: Dict[str, Any], candidate: Dict[str, Any], policy: GatePolicy) -> GateResult:
    base_cases = {c["id"]: c for c in baseline["cases"]}
    cand_cases = {c["id"]: c for c in candidate["cases"]}
    paired = [case_id for case_id in base_cases if case_id in cand_cases]
    missing = sorted(set(base_cases) - set(cand_cases))
    added = sorted(set(cand_cases) - set(base_cases))
    regressed = [i for i in paired if base_cases[i]["passed"] and not cand_cases[i]["passed"]]
    improved = [i for i in paired if not base_cases[i]["passed"] and cand_cases[i]["passed"]]
    both = sum(1 for i in paired if base_cases[i]["passed"] and cand_cases[i]["passed"])
    n = len(paired)
    b, c = len(regressed), len(improved)
    p_value = mcnemar_exact(b, c)
    effect = (c - b) / n if n else 0.0
    significant = p_value < policy.alpha
    if significant and effect <= -policy.min_effect:
        verdict = "regressed"
    elif significant and effect >= policy.min_effect:
        verdict = "improved"
    else:
        verdict = "no significant difference"
    base_rate = sum(1 for i in paired if base_cases[i]["passed"]) / n if n else 0.0
    cand_rate = sum(1 for i in paired if cand_cases[i]["passed"]) / n if n else 0.0
    summary = candidate.get("summary", {})
    result = GateResult(True, verdict)
    result.stats = {
        "baseline_model": baseline.get("model"),
        "candidate_model": candidate.get("model"),
        "paired": n,
        "both_pass": both,
        "regressed": regressed,
        "improved": improved,
        "p_value": p_value,
        "alpha": policy.alpha,
        "effect": round(effect, 6),
        "min_effect": policy.min_effect,
        "baseline_pass_rate": round(base_rate, 6),
        "candidate_pass_rate": round(cand_rate, 6),
        "baseline_wilson": [round(x, 6) for x in wilson_interval(round(base_rate * n), n)],
        "candidate_wilson": [round(x, 6) for x in wilson_interval(round(cand_rate * n), n)],
        "candidate_latency_p95_ms": summary.get("latency_p95_ms"),
        "candidate_mean_cost_usd": summary.get("mean_cost_usd"),
        "baseline_mean_cost_usd": baseline.get("summary", {}).get("mean_cost_usd"),
    }
    if n == 0:
        result.reasons.append("no paired cases: the two result files share no case id")
    if missing:
        result.reasons.append(f"{len(missing)} baseline case(s) missing from the candidate run: {', '.join(missing)}")
    if verdict == "regressed":
        result.reasons.append(f"significant regression: {b} case(s) went from pass to fail and {c} from fail to pass, McNemar exact p = {p_value:.4g} < {policy.alpha:g}, effect {effect:+.2f} beyond the minimum {policy.min_effect:.2f}")
    critical = [i for i in regressed if base_cases[i].get("critical") or cand_cases[i].get("critical")]
    if critical:
        result.reasons.append(f"critical case(s) regressed: {', '.join(critical)}")
    if policy.require_improvement and verdict != "improved":
        result.reasons.append(f"an improvement is required and the verdict is {verdict!r} (p = {p_value:.4g}, effect {effect:+.2f})")
    if candidate.get("summary", {}).get("errors"):
        result.reasons.append(f"the candidate run has {summary['errors']} provider error(s); rerun before gating")
    if policy.max_p95_latency_ms is not None and summary.get("latency_p95_ms", 0.0) > policy.max_p95_latency_ms:
        result.reasons.append(f"p95 latency {summary['latency_p95_ms']:.0f} ms over the budget of {policy.max_p95_latency_ms:.0f} ms")
    if policy.max_mean_cost_usd is not None and summary.get("mean_cost_usd", 0.0) > policy.max_mean_cost_usd:
        result.reasons.append(f"mean cost ${summary['mean_cost_usd']:.6f} per call over the budget of ${policy.max_mean_cost_usd:.6f}")
    if verdict == "no significant difference" and b > c:
        result.notes.append(f"the candidate lost {b - c} case(s) net but this suite cannot tell that from noise (p = {p_value:.4g}); add cases or samples before trusting it")
    if 0 < b + c < min_discordant_for(policy.alpha):
        result.notes.append(f"only {b + c} discordant case(s): McNemar cannot reach p < {policy.alpha:g} with fewer than {min_discordant_for(policy.alpha)}, whatever their direction")
    if added:
        result.notes.append(f"{len(added)} new case(s) only in the candidate run, not compared: {', '.join(added)}")
    if baseline.get("suite_sha256") != candidate.get("suite_sha256"):
        result.notes.append("the two runs used different suite files; only the shared case ids are compared")
    if baseline.get("samples") != candidate.get("samples"):
        result.notes.append(f"sample counts differ ({baseline.get('samples')} vs {candidate.get('samples')}); a case passes only when all its samples pass")
    result.passed = not result.reasons
    return result


def junit_xml(suites: List[Tuple[str, List[Tuple[str, str, Optional[str]]]]]) -> str:
    root = ET.Element("testsuites", name="minieval")
    total = failures = 0
    for suite_name, tests in suites:
        failed = sum(1 for _, _, failure in tests if failure is not None)
        element = ET.SubElement(root, "testsuite", name=suite_name, tests=str(len(tests)), failures=str(failed), errors="0")
        for classname, name, failure in tests:
            case = ET.SubElement(element, "testcase", classname=classname, name=name)
            if failure is not None:
                ET.SubElement(case, "failure", message=failure[:200]).text = failure
        total += len(tests)
        failures += failed
    root.set("tests", str(total))
    root.set("failures", str(failures))
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def run_junit(results: Dict[str, Any]) -> str:
    classname = f"{results['suite']}.{results['model']}"
    tests = [(classname, c["id"], None if c["passed"] else failure_detail(c)) for c in results["cases"]]
    return junit_xml([(f"{results['suite']} on {results['model']}", tests)])


def gate_junit(result: GateResult) -> str:
    stats = result.stats
    classname = f"gate.{stats.get('candidate_model')}"
    checks = [(classname, "release-gate", None if result.passed else "; ".join(result.reasons))]
    return junit_xml([(f"gate {stats.get('baseline_model')} -> {stats.get('candidate_model')}", checks)])


def normalize_words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def ngrams(words: Sequence[str], n: int) -> Set[Tuple[str, ...]]:
    if not words:
        return set()
    if len(words) < n:
        return {tuple(words)}
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def training_texts(path: str) -> List[str]:
    raw = Path(path).read_text()
    if not path.endswith(".jsonl"):
        return [raw]

    def strings(value: Any) -> Iterable[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)

    texts = []
    for number, line in enumerate(raw.splitlines(), 1):
        if line.strip():
            try:
                texts.append(" ".join(strings(json.loads(line))))
            except json.JSONDecodeError as exc:
                raise SuiteError(f"{path}:{number}: not JSON: {exc.msg}") from exc
    return texts


def contamination(suite: Suite, texts: Sequence[str], n: int = 8, threshold: float = 0.5) -> List[Dict[str, Any]]:
    corpus = [normalize_words(t) for t in texts]
    index: Dict[int, Set[Tuple[str, ...]]] = {}

    def grams_of_size(size: int) -> Set[Tuple[str, ...]]:
        if size not in index:
            index[size] = set()
            for words in corpus:
                index[size].update(tuple(words[i:i + size]) for i in range(len(words) - size + 1))
        return index[size]

    report = []
    for case in suite.cases:
        words = normalize_words(case.input + " " + (case.reference or ""))
        grams = ngrams(words, n)
        size = min(n, len(words))
        matched = sorted(g for g in grams if g in grams_of_size(size)) if grams else []
        overlap = len(matched) / len(grams) if grams else 0.0
        report.append({
            "id": case.id,
            "ngrams": len(grams),
            "matched": len(matched),
            "overlap": round(overlap, 4),
            "flagged": overlap >= threshold,
            "example": " ".join(matched[0]) if matched else "",
        })
    return report


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value))


def to_yaml(value: Any, indent: int = 0) -> List[str]:
    pad = " " * indent
    lines: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = key if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else json.dumps(key)
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{name}:")
                lines.extend(to_yaml(item, indent + 2))
            else:
                lines.append(f"{pad}{name}: {yaml_scalar(item) if not isinstance(item, (dict, list)) else ('{}' if isinstance(item, dict) else '[]')}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)) and item:
                nested = to_yaml(item, indent + 2)
                lines.append(f"{pad}- {nested[0].lstrip()}")
                lines.extend(nested[1:])
            else:
                lines.append(f"{pad}- {yaml_scalar(item)}")
    else:
        lines.append(f"{pad}{yaml_scalar(value)}")
    return lines


def numeric_javascript(value: float, tolerance: float) -> str:
    return f"Math.abs(parseFloat((String(output).replace(/,/g, '').match(/-?\\d+(?:\\.\\d+)?/) || ['NaN'])[0]) - ({value!r})) <= {tolerance!r} + 1e-12"


def promptfoo_assertion(assertion: Assertion) -> Dict[str, Any]:
    if assertion.type == "json-schema":
        return {"type": "is-json", "value": assertion.value}
    if assertion.type == "numeric":
        return {"type": "javascript", "value": numeric_javascript(float(assertion.value), assertion.tolerance)}
    if assertion.type == "rubric":
        return {"type": "llm-rubric", "value": assertion.value}
    return {"type": assertion.type, "value": assertion.value}


def promptfoo_config(suite: Suite, base_url: str, models: Sequence[str], judge_model: Optional[str] = None, api_key: str = "stub") -> Dict[str, Any]:
    def provider(model: str) -> Dict[str, Any]:
        return {"id": f"openai:chat:{model}", "label": model, "config": {"apiBaseUrl": base_url.rstrip("/"), "apiKey": api_key, "temperature": 0, "seed": 0}}

    config: Dict[str, Any] = {
        "description": f"{suite.name}: generated by minieval from {os.path.basename(suite.path) or 'the suite'}",
        "prompts": [TEMPLATE_INPUT.sub("{{input}}", suite.template)],
        "providers": [provider(m) for m in models],
    }
    if any(a.type == "rubric" for c in suite.cases for a in c.assertions):
        if not judge_model:
            raise SuiteError("the suite has rubric assertions; pass --judge-model so promptfoo's llm-rubric has a grader")
        config["defaultTest"] = {"options": {"provider": provider(judge_model), "rubricPrompt": JUDGE_TEMPLATE}}
    config["tests"] = [
        {"description": case.id, "vars": {"input": case.input}, "assert": [promptfoo_assertion(a) for a in case.assertions]}
        for case in suite.cases
    ]
    return config


@dataclass
class StubModel:
    answers: Dict[str, Any]


def load_stub_models(data: Dict[str, Any]) -> Tuple[Dict[str, StubModel], List[str]]:
    raw_models = data.get("models", {})
    resolved: Dict[str, StubModel] = {}

    def resolve(name: str, chain: Tuple[str, ...]) -> Dict[str, Any]:
        if name in chain:
            raise SuiteError(f"stub models: inherit loop {' -> '.join(chain + (name,))}")
        if name not in raw_models:
            raise SuiteError(f"stub models: {chain[-1] if chain else '?'} inherits unknown model {name!r}")
        spec = raw_models[name]
        answers = dict(resolve(spec["inherit"], chain + (name,))) if spec.get("inherit") else {}
        answers.update(spec.get("answers", {}))
        return answers

    for name in raw_models:
        resolved[name] = StubModel(resolve(name, ()))
    return resolved, list(data.get("judge_models", []))


def approx_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def stub_answer(suite: Suite, model: StubModel, prompt: str, seed: int) -> str:
    matches = [case for case in suite.cases if case.input in prompt]
    if not matches:
        return FALLBACK_ANSWER
    case = max(matches, key=lambda c: len(c.input))
    answer = model.answers.get(case.id, FALLBACK_ANSWER)
    if isinstance(answer, list):
        return str(answer[seed % len(answer)])
    return str(answer)


def message_text(messages: Any) -> str:
    parts: List[str] = []
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return "\n".join(parts)


def stub_completion(suite: Suite, models: Dict[str, StubModel], judges: List[str], body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    model = body.get("model")
    prompt = message_text(body.get("messages"))
    if model in judges:
        parts = split_judge_prompt(prompt)
        if parts is None:
            text = json.dumps({"reason": "no <Output> and <Rubric> in the prompt", "pass": False, "score": 0.0})
        else:
            verdict = keyword_judge(*parts)
            text = json.dumps({"reason": verdict.reason, "pass": verdict.passed, "score": verdict.score})
    elif model in models:
        seed = body.get("seed")
        text = stub_answer(suite, models[model], prompt, int(seed) if isinstance(seed, int) else 0)
    else:
        return 404, {"error": {"message": f"The model '{model}' does not exist", "type": "invalid_request_error", "code": "model_not_found"}}
    prompt_tokens, completion_tokens = approx_tokens(prompt), approx_tokens(text)
    return 200, {
        "id": "chatcmpl-" + hashlib.sha256((str(model) + prompt + text).encode()).hexdigest()[:24],
        "object": "chat.completion",
        "created": 1767225600,
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens},
    }


def make_stub_server(suite: Suite, answers: Dict[str, Any], host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    models, judges = load_stub_models(answers)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return

        def send_json(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/v1/models":
                data = [{"id": name, "object": "model", "owned_by": "minieval-stub"} for name in list(models) + judges]
                self.send_json(200, {"object": "list", "data": data})
            else:
                self.send_json(404, {"error": {"message": f"no route {self.path}", "type": "invalid_request_error"}})

        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/v1/chat/completions":
                self.send_json(404, {"error": {"message": f"no route {self.path}", "type": "invalid_request_error"}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode() or "{}")
            except (ValueError, UnicodeDecodeError):
                self.send_json(400, {"error": {"message": "body is not JSON", "type": "invalid_request_error"}})
                return
            status, payload = stub_completion(suite, models, judges, body if isinstance(body, dict) else {})
            self.send_json(status, payload)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def render_run(results: Dict[str, Any]) -> str:
    s = results["summary"]
    lines = [f"suite {results['suite']}, model {results['model']}, {s['cases']} cases x {results['samples']} sample(s)"]
    for case in results["cases"]:
        flag = "PASS" if case["passed"] else "FAIL"
        extra = "" if case["passed"] else "  " + failure_detail(case)[:140]
        if results["samples"] > 1:
            extra = f"  pass@1 {case['pass_at']['1']:.2f} pass@{results['samples']} {case['pass_at'][str(results['samples'])]:.2f}" + extra
        lines.append(f"  {flag} {case['id']}{' (critical)' if case['critical'] else ''}{extra}")
    pass_at = ", ".join(f"pass@{k} {v:.3f}" for k, v in s["pass_at"].items())
    lines.append(f"pass rate {s['passed']}/{s['cases']} = {s['pass_rate']:.2f} ({int(s['confidence'] * 100)}% Wilson {s['wilson_low']:.2f}-{s['wilson_high']:.2f}); {pass_at}")
    lines.append(f"latency p50 {s['latency_p50_ms']:.1f} ms p95 {s['latency_p95_ms']:.1f} ms; cost ${s['mean_cost_usd']:.6f} per call, ${s['total_cost_usd']:.6f} total; provider errors {s['errors']}")
    return "\n".join(lines)


def render_gate(result: GateResult) -> str:
    s = result.stats
    lines = [
        f"gate {s['baseline_model']} -> {s['candidate_model']} on {s['paired']} paired cases: {'ACCEPT' if result.passed else 'REJECT'} (quality: {result.verdict})",
        f"  baseline {s['baseline_pass_rate']:.2f} [{s['baseline_wilson'][0]:.2f}, {s['baseline_wilson'][1]:.2f}]  candidate {s['candidate_pass_rate']:.2f} [{s['candidate_wilson'][0]:.2f}, {s['candidate_wilson'][1]:.2f}]",
        f"  discordant: {len(s['regressed'])} pass->fail, {len(s['improved'])} fail->pass; McNemar exact p = {s['p_value']:.4g} (alpha {s['alpha']:g}); effect {s['effect']:+.2f} (min {s['min_effect']:.2f})",
    ]
    if s["regressed"]:
        lines.append("  pass->fail: " + ", ".join(s["regressed"]))
    if s["improved"]:
        lines.append("  fail->pass: " + ", ".join(s["improved"]))
    lines += [f"  reason: {r}" for r in result.reasons]
    lines += [f"  note: {n}" for n in result.notes]
    return "\n".join(lines)


def write_text(path: str, text: str) -> None:
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def load_results(path: str) -> Dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise SuiteError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or "cases" not in data:
        raise SuiteError(f"{path}: not a minieval results file (no 'cases')")
    return data


def cmd_run(args: argparse.Namespace) -> int:
    suite = load_suite(args.suite)
    temperature = args.temperature if args.temperature is not None else (0.0 if args.samples == 1 else 0.7)
    provider = OpenAIChatProvider(args.base_url, args.model, os.environ.get(args.api_key_env, ""), args.timeout)
    judge: Optional[Judge] = None
    judge_name = ""
    if args.judge == "keyword":
        judge, judge_name = keyword_judge, "keyword"
    elif args.judge:
        judge = model_judge(OpenAIChatProvider(args.judge_base_url or args.base_url, args.judge, os.environ.get(args.api_key_env, ""), args.timeout))
        judge_name = args.judge
    results = run_suite(suite, provider, args.samples, temperature, judge, judge_name)
    if args.out:
        write_text(args.out, json.dumps(results, indent=2) + "\n")
    if args.junit:
        write_text(args.junit, run_junit(results))
    print(json.dumps(results, indent=2) if args.json else render_run(results))
    summary = results["summary"]
    if summary["errors"]:
        print(f"minieval: {summary['errors']} call(s) failed; an eval that cannot reach the model is not a 0% model", file=sys.stderr)
        return 1
    return 0 if summary["pass_rate"] >= args.min_pass_rate else 1


def cmd_gate(args: argparse.Namespace) -> int:
    baseline, candidate = load_results(args.baseline), load_results(args.candidate)
    policy = GatePolicy(**candidate.get("gate", {}))
    for name in ("alpha", "min_effect", "max_p95_latency_ms", "max_mean_cost_usd"):
        if getattr(args, name) is not None:
            setattr(policy, name, getattr(args, name))
    if args.require_improvement:
        policy.require_improvement = True
    result = gate(baseline, candidate, policy)
    if args.junit:
        write_text(args.junit, gate_junit(result))
    print(json.dumps(asdict(result), indent=2) if args.json else render_gate(result))
    return 0 if result.passed else 1


def cmd_contamination(args: argparse.Namespace) -> int:
    suite = load_suite(args.suite)
    report = contamination(suite, training_texts(args.train), args.n, args.threshold)
    flagged = [r for r in report if r["flagged"]]
    if args.json:
        print(json.dumps({"n": args.n, "threshold": args.threshold, "cases": report, "flagged": [r["id"] for r in flagged]}, indent=2))
    else:
        for row in report:
            if row["matched"]:
                print(f"  {'FLAG' if row['flagged'] else 'seen'} {row['id']}: {row['matched']}/{row['ngrams']} {args.n}-grams in the training set ({row['overlap']:.0%}), e.g. {row['example']!r}")
        print(f"{len(flagged)} of {len(report)} case(s) at or above {args.threshold:.0%} {args.n}-gram overlap")
    return 1 if flagged else 0


def cmd_promptfoo_config(args: argparse.Namespace) -> int:
    suite = load_suite(args.suite)
    text = "\n".join(to_yaml(promptfoo_config(suite, args.base_url, args.model, args.judge_model))) + "\n"
    if args.out:
        write_text(args.out, text)
        print(args.out)
    else:
        sys.stdout.write(text)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    if args.test == "mcnemar":
        b, c = args.numbers
        result: Dict[str, Any] = {"b": b, "c": c, "p_value": mcnemar_exact(b, c)}
    elif args.test == "wilson":
        k, n = args.numbers
        low, high = wilson_interval(k, n, args.confidence)
        result = {"successes": k, "trials": n, "confidence": args.confidence, "low": low, "high": high}
    else:
        n, c, k = args.numbers
        result = {"n": n, "c": c, "k": k, "pass_at_k": pass_at_k(n, c, k)}
    print(json.dumps(result) if args.json else " ".join(f"{key}={value}" for key, value in result.items()))
    return 0


def cmd_serve_stub(args: argparse.Namespace) -> int:
    suite = load_suite(args.suite)
    answers = json.loads(Path(args.answers).read_text())
    server = make_stub_server(suite, answers, args.host, args.port)
    host, port = server.server_address[:2]
    if args.port_file:
        tmp = args.port_file + ".tmp"
        Path(tmp).write_text(f"{port}\n")
        os.replace(tmp, args.port_file)
    print(f"minieval stub: http://{host}:{port}/v1 serving {', '.join(sorted(answers.get('models', {})))} and judge {', '.join(answers.get('judge_models', []))}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minieval", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a suite against one model and write results")
    run.add_argument("--suite", required=True)
    run.add_argument("--base-url", required=True, help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8000/v1")
    run.add_argument("--model", required=True)
    run.add_argument("--samples", type=int, default=1, help="samples per case (seeds 0..N-1) for pass@k")
    run.add_argument("--temperature", type=float, default=None)
    run.add_argument("--judge", default="", help="'keyword' for the deterministic judge, or a judge model name served at --judge-base-url")
    run.add_argument("--judge-base-url", default="")
    run.add_argument("--api-key-env", default="OPENAI_API_KEY")
    run.add_argument("--timeout", type=float, default=60.0)
    run.add_argument("--min-pass-rate", type=float, default=0.0)
    run.add_argument("--out", help="write results JSON here")
    run.add_argument("--junit", help="write a JUnit XML report here")
    run.add_argument("--json", action="store_true")

    gate_cmd = sub.add_parser("gate", help="accept or reject a candidate run against a baseline run")
    gate_cmd.add_argument("--baseline", required=True)
    gate_cmd.add_argument("--candidate", required=True)
    gate_cmd.add_argument("--alpha", type=float)
    gate_cmd.add_argument("--min-effect", type=float)
    gate_cmd.add_argument("--max-p95-latency-ms", type=float)
    gate_cmd.add_argument("--max-mean-cost-usd", type=float)
    gate_cmd.add_argument("--require-improvement", action="store_true")
    gate_cmd.add_argument("--junit")
    gate_cmd.add_argument("--json", action="store_true")

    cont = sub.add_parser("contamination", help="n-gram overlap between eval cases and a training set")
    cont.add_argument("--suite", required=True)
    cont.add_argument("--train", required=True, help="a .jsonl file (every string field is text) or a plain text file")
    cont.add_argument("--n", type=int, default=8)
    cont.add_argument("--threshold", type=float, default=0.5)
    cont.add_argument("--json", action="store_true")

    pf = sub.add_parser("promptfoo-config", help="write the suite as a promptfoo config")
    pf.add_argument("--suite", required=True)
    pf.add_argument("--base-url", required=True)
    pf.add_argument("--model", action="append", required=True)
    pf.add_argument("--judge-model")
    pf.add_argument("--out")

    stats = sub.add_parser("stats", help="mcnemar B C | wilson K N | pass-at-k N C K")
    stats.add_argument("test", choices=["mcnemar", "wilson", "pass-at-k"])
    stats.add_argument("numbers", type=int, nargs="+")
    stats.add_argument("--confidence", type=float, default=0.95)
    stats.add_argument("--json", action="store_true")

    stub = sub.add_parser("serve-stub", help="serve scripted models over an OpenAI-compatible API")
    stub.add_argument("--suite", required=True)
    stub.add_argument("--answers", required=True)
    stub.add_argument("--host", default="127.0.0.1")
    stub.add_argument("--port", type=int, default=0)
    stub.add_argument("--port-file")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command == "stats":
        wanted = {"mcnemar": 2, "wilson": 2, "pass-at-k": 3}[args.test]
        if len(args.numbers) != wanted:
            print(f"minieval: stats {args.test} takes {wanted} numbers", file=sys.stderr)
            return 2
    handlers = {"run": cmd_run, "gate": cmd_gate, "contamination": cmd_contamination, "promptfoo-config": cmd_promptfoo_config, "stats": cmd_stats, "serve-stub": cmd_serve_stub}
    try:
        return handlers[args.command](args)
    except (SuiteError, ValueError) as exc:
        print(f"minieval: {exc}", file=sys.stderr)
        return 2
    except ProviderError as exc:
        print(f"minieval: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"minieval: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
