"""minirollout: a model router and rollout controller. It serves a primary model version, mirrors requests to a shadow version whose answers are recorded and never returned, sends a sticky, weighted share of users to a canary, records latency, errors, agreement and user feedback per version, and promotes, holds or rolls back with a Mann-Whitney U test, two-proportion z-tests, a minimum effect and a minimum sample size. On promotion it moves the registry alias (a JSON file or MLflow's REST API). The same file runs stub model servers and a load generator whose /compare endpoint feeds Argo Rollouts' web metric provider."""
from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import http.client
import itertools
import json
import math
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

PROMOTE, HOLD, ROLLBACK = "promote", "hold", "rollback"
PASS, FAIL, SUSPECT, INSUFFICIENT = "pass", "fail", "suspect", "insufficient"
SERVED, SHADOW = "served", "shadow"
PRIMARY, CANARY = "primary", "canary"
PROMOTED, ROLLED_BACK, HELD, PROGRESSING = "Promoted", "RolledBack", "Held", "Progressing"
EXIT_CODES = {PROMOTE: 0, ROLLBACK: 1, HOLD: 3}
PHASE_EXIT = {PROMOTED: 0, ROLLED_BACK: 1, HELD: 3}
CHAMPION = "champion"
TRUE_WEIGHTS = (1.5, -2.0, 0.25)
LABEL_NOISE = 0.03
DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class PlanError(ValueError):
    pass


class UpstreamError(Exception):
    pass


class RegistryError(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Example:
    id: str
    features: Tuple[float, float]
    label: int


def score(weights: Sequence[float], features: Sequence[float]) -> float:
    return weights[0] * features[0] + weights[1] * features[1] + weights[2]


def predict(weights: Sequence[float], features: Sequence[float]) -> Tuple[int, float]:
    z = max(-60.0, min(60.0, score(weights, features)))
    probability = 1.0 / (1.0 + math.exp(-z))
    return (1 if probability >= 0.5 else 0), round(probability, 6)


def make_dataset(seed: int = 7, size: int = 1000) -> List[Example]:
    rng = random.Random(seed)
    examples = []
    for index in range(size):
        features = (round(rng.uniform(-2.0, 2.0), 4), round(rng.uniform(-2.0, 2.0), 4))
        label = 1 if score(TRUE_WEIGHTS, features) > 0 else 0
        if rng.random() < LABEL_NOISE:
            label = 1 - label
        examples.append(Example(f"x{index}", features, label))
    return examples


def accuracy(weights: Sequence[float], examples: Sequence[Example]) -> float:
    return sum(predict(weights, example.features)[0] == example.label for example in examples) / len(examples)


def parse_weights(text: str) -> Tuple[float, float, float]:
    try:
        values = tuple(float(part) for part in text.split(","))
    except ValueError:
        raise ValueError(f"weights must be three numbers like 1.5,-2,0.25, got {text!r}") from None
    if len(values) != 3:
        raise ValueError(f"weights must be three numbers (w1, w2, bias), got {len(values)} in {text!r}")
    return values[0], values[1], values[2]


def fraction(*parts: str) -> float:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2.0 ** 64


def bucket(user: str, salt: str) -> float:
    return fraction("cohort", salt, user) * 100.0


def assign(user: str, salt: str, weight: float) -> str:
    return CANARY if weight > 0 and bucket(user, salt) < weight else PRIMARY


@dataclass
class ModelBehaviour:
    version: str
    weights: Tuple[float, float, float] = TRUE_WEIGHTS
    latency_ms: float = 5.0
    jitter_ms: float = 5.0
    error_rate: float = 0.0

    def answer(self, body: Any, request_id: str = "") -> Tuple[int, Dict[str, Any], float]:
        features = body.get("features") if isinstance(body, dict) else None
        if not isinstance(features, list) or len(features) != 2 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in features):
            return 400, {"version": self.version, "error": "features must be a list of two numbers"}, 0.0
        key = request_id or str(body.get("id") or json.dumps(features))
        delay = (self.latency_ms + self.jitter_ms * fraction(self.version, "latency", key)) / 1000.0
        if self.error_rate > 0 and fraction(self.version, "error", key) < self.error_rate:
            return 500, {"version": self.version, "error": "injected failure"}, delay
        label, probability = predict(self.weights, features)
        return 200, {"version": self.version, "label": label, "score": probability}, delay


@dataclass
class Sample:
    t: float
    version: str
    role: str
    latency_ms: float
    ok: bool
    request_id: str = ""
    user: str = ""
    label: Optional[int] = None
    agree: Optional[bool] = None
    feedback: Optional[bool] = None

    def to_json(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: Any) -> "Sample":
        if not isinstance(data, dict):
            raise ValueError(f"a sample is an object, got {data!r}")
        missing = [name for name in ("t", "version", "role", "latency_ms", "ok") if name not in data]
        if missing:
            raise ValueError(f"sample is missing {', '.join(missing)}: {data}")
        names = [f.name for f in dataclasses.fields(cls)]
        return cls(**{name: data[name] for name in names if name in data})


def in_window(sample: Sample, version: Optional[str], role: Optional[str], since: Optional[float], until: Optional[float]) -> bool:
    return ((version is None or sample.version == version) and (role is None or sample.role == role)
            and (since is None or sample.t > since) and (until is None or sample.t <= until))


def percentile(ordered: Sequence[float], p: float) -> float:
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def summarize(samples: Sequence[Sample]) -> Dict[str, Any]:
    count = len(samples)
    errors = sum(1 for s in samples if not s.ok)
    latencies = sorted(s.latency_ms for s in samples if s.ok)
    feedback = [s.feedback for s in samples if s.feedback is not None]
    agreement = [s.agree for s in samples if s.agree is not None]
    return {
        "requests": count,
        "errors": errors,
        "error_rate": round(errors / count, 6) if count else 0.0,
        "p50_ms": round(percentile(latencies, 50), 3),
        "p95_ms": round(percentile(latencies, 95), 3),
        "feedback": len(feedback),
        "feedback_rate": round(sum(feedback) / len(feedback), 6) if feedback else 0.0,
        "agreement": len(agreement),
        "agreement_rate": round(sum(agreement) / len(agreement), 6) if agreement else 0.0,
        "users": len({s.user for s in samples if s.user}),
    }


class Recorder:
    def __init__(self, clock: Callable[[], float] = time.time, limit: int = 200000):
        self.clock = clock
        self.limit = limit
        self.lock = threading.Lock()
        self.items: "collections.deque[Sample]" = collections.deque(maxlen=limit)
        self.by_request: "collections.OrderedDict[str, Sample]" = collections.OrderedDict()
        self.queries: "collections.deque[Dict[str, Any]]" = collections.deque(maxlen=10000)

    def add(self, sample: Sample) -> None:
        with self.lock:
            self.items.append(sample)
            if sample.role == SERVED and sample.request_id:
                self.by_request[sample.request_id] = sample
                while len(self.by_request) > self.limit:
                    self.by_request.popitem(last=False)

    def feedback(self, request_id: str, good: bool) -> bool:
        with self.lock:
            sample = self.by_request.get(request_id)
            if sample is None:
                return False
            sample.feedback = bool(good)
            return True

    def select(self, version: Optional[str] = None, role: Optional[str] = None, since: Optional[float] = None, until: Optional[float] = None) -> List[Sample]:
        with self.lock:
            items = list(self.items)
        return [s for s in items if in_window(s, version, role, since, until)]

    def compare(self, primary: str, candidate: str, window: float, role: str = SERVED, step: str = "", until: Optional[float] = None) -> Dict[str, Any]:
        end = self.clock() if until is None else until
        start = end - window
        entry = {"since": start, "until": end, "window": window, "primary": primary, "candidate": candidate, "role": role, "step": step}
        with self.lock:
            self.queries.append(dict(entry))
        result = dict(entry)
        result["primary"] = dict(summarize(self.select(primary, SERVED, start, end)), version=primary)
        result["candidate"] = dict(summarize(self.select(candidate, role, start, end)), version=candidate)
        return result

    def logged_queries(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [dict(q) for q in self.queries]


def norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def norm_ppf(p: float) -> float:
    if not 0.0 < p < 1.0:
        raise ValueError(f"a probability strictly between 0 and 1 is needed, got {p}")
    low, high = -40.0, 40.0
    for _ in range(200):
        mid = (low + high) / 2.0
        if 0.5 * math.erfc(-mid / math.sqrt(2.0)) < p:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


@dataclass
class TestResult:
    statistic: float
    z: float
    p_value: float


def midranks(values: Sequence[float]) -> Tuple[List[float], List[int]]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    ties = []
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        for position in range(start, end + 1):
            ranks[order[position]] = (start + end) / 2.0 + 1.0
        ties.append(end - start + 1)
        start = end + 1
    return ranks, ties


def mann_whitney_greater(candidate: Sequence[float], primary: Sequence[float]) -> TestResult:
    n1, n2 = len(candidate), len(primary)
    if n1 == 0 or n2 == 0:
        raise ValueError("the Mann-Whitney U test needs at least one value on each side")
    ranks, ties = midranks(list(candidate) + list(primary))
    u = sum(ranks[:n1]) - n1 * (n1 + 1) / 2.0
    n = n1 + n2
    tie_term = sum(t ** 3 - t for t in ties)
    variance = n1 * n2 / 12.0 * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0.0
    if variance <= 0:
        return TestResult(u, 0.0, 1.0)
    z = (u - n1 * n2 / 2.0 - 0.5) / math.sqrt(variance)
    return TestResult(u, z, norm_sf(z))


def two_proportion_greater(bad_candidate: int, n_candidate: int, bad_primary: int, n_primary: int) -> TestResult:
    if n_candidate <= 0 or n_primary <= 0:
        raise ValueError("the two-proportion z-test needs at least one observation on each side")
    difference = bad_candidate / n_candidate - bad_primary / n_primary
    pooled = (bad_candidate + bad_primary) / (n_candidate + n_primary)
    variance = pooled * (1 - pooled) * (1.0 / n_candidate + 1.0 / n_primary)
    if variance <= 0:
        return TestResult(difference, 0.0, 1.0)
    z = difference / math.sqrt(variance)
    return TestResult(difference, z, norm_sf(z))


def one_proportion_greater(bad: int, n: int, limit: float) -> TestResult:
    if n <= 0:
        raise ValueError("the one-proportion z-test needs at least one observation")
    if not 0.0 < limit < 1.0:
        raise ValueError(f"the limit must be strictly between 0 and 1, got {limit}")
    rate = bad / n
    z = (rate - limit) / math.sqrt(limit * (1 - limit) / n)
    return TestResult(rate, z, norm_sf(z))


def samples_needed(baseline: float, drop: float, alpha: float = 0.01, power: float = 0.8) -> int:
    worse = baseline - drop
    if not 0.0 < worse < baseline < 1.0:
        raise ValueError(f"need 0 < baseline - drop < baseline < 1, got baseline {baseline} and drop {drop}")
    mean = (baseline + worse) / 2.0
    numerator = norm_ppf(1 - alpha) * math.sqrt(2 * mean * (1 - mean)) + norm_ppf(power) * math.sqrt(baseline * (1 - baseline) + worse * (1 - worse))
    return math.ceil((numerator / drop) ** 2)


@dataclass
class Thresholds:
    alpha: float = 0.01
    min_samples: int = 150
    latency_ms: float = 50.0
    error_rate: float = 0.02
    quality_drop: float = 0.10
    disagreement: float = 0.10


@dataclass
class Check:
    metric: str
    test: str
    n_primary: int
    n_candidate: int
    primary: float
    candidate: float
    effect: float
    limit: float
    z: float
    p_value: float
    verdict: str
    reason: str

    def to_json(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class Analysis:
    mode: str
    decision: str
    checks: List[Check]

    def reasons(self) -> List[str]:
        return [c.reason for c in self.checks if c.verdict != PASS]

    def to_json(self) -> Dict[str, Any]:
        return {"mode": self.mode, "decision": self.decision, "reasons": self.reasons(), "checks": [c.to_json() for c in self.checks]}


def judge(n_primary: int, n_candidate: int, effect: float, limit: float, p_value: float, th: Thresholds) -> str:
    if min(n_primary, n_candidate) < th.min_samples:
        return INSUFFICIENT
    if effect > limit and p_value < th.alpha:
        return FAIL
    if effect > limit:
        return SUSPECT
    return PASS


def make_check(metric: str, test: str, n_primary: int, n_candidate: int, primary: float, candidate: float, effect: float, limit: float, result: TestResult, th: Thresholds, unit: str = "") -> Check:
    verdict = judge(n_primary, n_candidate, effect, limit, result.p_value, th)
    if verdict == INSUFFICIENT:
        reason = f"{metric} {verdict}: {min(n_primary, n_candidate)} < {th.min_samples} samples"
    else:
        reason = (f"{metric} {verdict}: candidate {candidate:.4g}{unit} vs primary {primary:.4g}{unit}, "
                  f"effect {effect:+.4g}{unit} (limit {limit:g}{unit}), {test} z={result.z:.2f} p={result.p_value:.2g}")
    return Check(metric, test, n_primary, n_candidate, round(primary, 6), round(candidate, 6), round(effect, 6), limit,
                 round(result.z, 6), result.p_value, verdict, reason)


def check_latency(primary: Sequence[Sample], candidate: Sequence[Sample], th: Thresholds) -> Check:
    p_lat = sorted(s.latency_ms for s in primary if s.ok)
    c_lat = sorted(s.latency_ms for s in candidate if s.ok)
    p_median, c_median = percentile(p_lat, 50), percentile(c_lat, 50)
    result = mann_whitney_greater(c_lat, p_lat) if p_lat and c_lat else TestResult(0.0, 0.0, 1.0)
    return make_check("latency", "Mann-Whitney U", len(p_lat), len(c_lat), p_median, c_median, c_median - p_median, th.latency_ms, result, th, " ms")


def check_errors(primary: Sequence[Sample], candidate: Sequence[Sample], th: Thresholds) -> Check:
    p_bad, c_bad = sum(1 for s in primary if not s.ok), sum(1 for s in candidate if not s.ok)
    p_rate = p_bad / len(primary) if primary else 0.0
    c_rate = c_bad / len(candidate) if candidate else 0.0
    result = two_proportion_greater(c_bad, len(candidate), p_bad, len(primary)) if primary and candidate else TestResult(0.0, 0.0, 1.0)
    return make_check("errors", "two-proportion z", len(primary), len(candidate), p_rate, c_rate, c_rate - p_rate, th.error_rate, result, th)


def check_quality(primary: Sequence[Sample], candidate: Sequence[Sample], th: Thresholds) -> Check:
    p_votes = [s.feedback for s in primary if s.feedback is not None]
    c_votes = [s.feedback for s in candidate if s.feedback is not None]
    p_rate = sum(p_votes) / len(p_votes) if p_votes else 0.0
    c_rate = sum(c_votes) / len(c_votes) if c_votes else 0.0
    if p_votes and c_votes:
        result = two_proportion_greater(len(c_votes) - sum(c_votes), len(c_votes), len(p_votes) - sum(p_votes), len(p_votes))
    else:
        result = TestResult(0.0, 0.0, 1.0)
    return make_check("quality", "two-proportion z", len(p_votes), len(c_votes), p_rate, c_rate, p_rate - c_rate, th.quality_drop, result, th)


def check_agreement(candidate: Sequence[Sample], th: Thresholds) -> Check:
    votes = [s.agree for s in candidate if s.agree is not None]
    disagreements = len(votes) - sum(votes)
    rate = disagreements / len(votes) if votes else 0.0
    result = one_proportion_greater(disagreements, len(votes), th.disagreement) if votes else TestResult(0.0, 0.0, 1.0)
    return make_check("disagreement", "one-proportion z", len(votes), len(votes), 0.0, rate, rate, th.disagreement, result, th)


def decide(checks: Sequence[Check]) -> str:
    verdicts = {c.verdict for c in checks}
    if FAIL in verdicts:
        return ROLLBACK
    if verdicts & {INSUFFICIENT, SUSPECT}:
        return HOLD
    return PROMOTE


def analyze(primary: Sequence[Sample], candidate: Sequence[Sample], th: Optional[Thresholds] = None, mode: str = SERVED) -> Analysis:
    th = th or Thresholds()
    checks = [check_latency(primary, candidate, th), check_errors(primary, candidate, th)]
    checks.append(check_agreement(candidate, th) if mode == SHADOW else check_quality(primary, candidate, th))
    return Analysis(mode, decide(checks), checks)


def http_json(method: str, url: str, payload: Any = None, headers: Optional[Dict[str, str]] = None, timeout: float = 5.0) -> Tuple[int, Any, Dict[str, str]]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    all_headers = dict(headers or {})
    if data is not None:
        all_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    try:
        with OPENER.open(request, timeout=timeout) as response:
            status, raw, response_headers = response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        status, raw, response_headers = exc.code, exc.read(), dict(exc.headers.items()) if exc.headers else {}
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise UpstreamError(f"{method} {url}: {getattr(exc, 'reason', exc)}") from None
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {"raw": raw.decode("utf-8", "replace")}
    return status, body, {k.lower(): v for k, v in response_headers.items()}


def post_json(url: str, payload: Any, headers: Dict[str, str]) -> Tuple[int, Any]:
    status, body, _ = http_json("POST", url, payload, headers)
    return status, body


AppResult = Tuple[int, Any, Dict[str, str]]
App = Callable[[str, str, Dict[str, str], Any, Dict[str, str]], AppResult]


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            pass

        def handle_method(self, method: str) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            try:
                body = json.loads(raw) if raw else {}
                status, payload, extra = app(method, parsed.path, query, body, headers)
            except ValueError as exc:
                status, payload, extra = 400, {"error": str(exc)}, {}
            except ApiError as exc:
                status, payload, extra = exc.status, {"error": str(exc)}, {}
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self.handle_method("GET")

        def do_POST(self) -> None:
            self.handle_method("POST")

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def server_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[:2]
    return f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"


def start_server(app: App, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    server = make_server(app, host, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def model_app(model: ModelBehaviour, sleep: Callable[[float], None] = time.sleep) -> App:
    def app(method: str, path: str, query: Dict[str, str], body: Any, headers: Dict[str, str]) -> AppResult:
        extra = {"X-Model-Version": model.version}
        if method == "GET" and path == "/healthz":
            return 200, {"version": model.version, "weights": list(model.weights)}, extra
        if method == "POST" and path == "/predict":
            status, payload, delay = model.answer(body, headers.get("x-request-id", ""))
            sleep(delay)
            return status, payload, extra
        raise ApiError(404, f"no route for {method} {path}")
    return app


def query_float(query: Dict[str, str], name: str, default: Optional[float] = None) -> Optional[float]:
    if name not in query:
        return default
    try:
        return float(query[name])
    except ValueError:
        raise ApiError(400, f"{name} must be a number, got {query[name]!r}") from None


def stats_app(recorder: Recorder) -> App:
    def app(method: str, path: str, query: Dict[str, str], body: Any, headers: Dict[str, str]) -> AppResult:
        if method == "GET" and path == "/healthz":
            return 200, {"ok": True}, {}
        if method == "GET" and path == "/stats":
            window = query_float(query, "window", 20.0) or 0.0
            end = recorder.clock()
            samples = recorder.select(query.get("version"), query.get("role", SERVED), end - window, end)
            return 200, dict(summarize(samples), version=query.get("version"), since=end - window, until=end), {}
        if method == "GET" and path == "/compare":
            if not query.get("primary") or not query.get("candidate"):
                raise ApiError(400, "compare needs primary and candidate query parameters")
            role = query.get("role", SERVED)
            if role not in (SERVED, SHADOW):
                raise ApiError(400, f"role must be {SERVED} or {SHADOW}, got {role!r}")
            return 200, recorder.compare(query["primary"], query["candidate"], query_float(query, "window", 20.0) or 0.0, role, query.get("step", "")), {}
        if method == "GET" and path == "/samples":
            samples = recorder.select(query.get("version"), query.get("role"), query_float(query, "since"), query_float(query, "until"))
            return 200, {"samples": [s.to_json() for s in samples]}, {}
        if method == "GET" and path == "/queries":
            return 200, {"queries": recorder.logged_queries()}, {}
        if method == "POST" and path == "/feedback":
            if not isinstance(body, dict) or not body.get("request_id") or not isinstance(body.get("good"), bool):
                raise ApiError(400, 'feedback is {"request_id": "...", "good": true|false}')
            if not recorder.feedback(str(body["request_id"]), body["good"]):
                raise ApiError(404, f"no served request {body['request_id']} (too old or never routed here)")
            return 200, {"ok": True}, {}
        raise ApiError(404, f"no route for {method} {path}")
    return app


@dataclass
class Backend:
    name: str
    url: str

    def to_json(self) -> Dict[str, str]:
        return {"name": self.name, "url": self.url}

    @classmethod
    def parse(cls, text: str) -> "Backend":
        name, sep, url = text.partition("=")
        if not sep or not name or not url.startswith(("http://", "https://")):
            raise ValueError(f"expected NAME=URL like v1=http://127.0.0.1:8001, got {text!r}")
        return cls(name, url.rstrip("/"))

    @classmethod
    def from_json(cls, data: Any) -> Optional["Backend"]:
        if data is None:
            return None
        if not isinstance(data, dict) or not data.get("name") or not str(data.get("url", "")).startswith(("http://", "https://")):
            raise ValueError(f"a backend needs a name and an http(s) url, got {data!r}")
        return cls(str(data["name"]), str(data["url"]).rstrip("/"))


@dataclass
class RouteConfig:
    primary: Backend
    canary: Optional[Backend] = None
    weight: float = 0.0
    shadow: Optional[Backend] = None
    shadow_percent: float = 0.0
    salt: str = ""

    def cohort_salt(self) -> str:
        return self.salt or (self.canary.name if self.canary else "")

    def validate(self) -> "RouteConfig":
        for name, value in (("weight", self.weight), ("shadow_percent", self.shadow_percent)):
            if not 0.0 <= value <= 100.0:
                raise ValueError(f"{name} must be between 0 and 100, got {value:g}")
        if self.weight > 0 and self.canary is None:
            raise ValueError("a weight above 0 needs a canary backend")
        if self.shadow_percent > 0 and self.shadow is None:
            raise ValueError("a shadow_percent above 0 needs a shadow backend")
        for role, backend in (("canary", self.canary), ("shadow", self.shadow)):
            if backend is not None and backend.name == self.primary.name:
                raise ValueError(f"the {role} must be a different version from the primary {self.primary.name}")
        return self

    def to_json(self) -> Dict[str, Any]:
        return {"primary": self.primary.to_json(), "canary": self.canary.to_json() if self.canary else None, "weight": self.weight,
                "shadow": self.shadow.to_json() if self.shadow else None, "shadow_percent": self.shadow_percent, "salt": self.salt}

    @classmethod
    def from_json(cls, data: Any) -> "RouteConfig":
        if not isinstance(data, dict) or data.get("primary") is None:
            raise ValueError("a route config needs a primary backend")
        try:
            weight, shadow_percent = float(data.get("weight", 0) or 0), float(data.get("shadow_percent", 0) or 0)
        except (TypeError, ValueError):
            raise ValueError(f"weight and shadow_percent must be numbers, got {data.get('weight')!r} and {data.get('shadow_percent')!r}") from None
        primary = Backend.from_json(data["primary"])
        assert primary is not None
        return cls(primary, Backend.from_json(data.get("canary")), weight, Backend.from_json(data.get("shadow")), shadow_percent, str(data.get("salt") or "")).validate()


class Router:
    def __init__(self, config: RouteConfig, recorder: Optional[Recorder] = None, post: Callable[[str, Any, Dict[str, str]], Tuple[int, Any]] = post_json,
                 timer: Callable[[], float] = time.perf_counter, mirror_workers: int = 2, queue_size: int = 1000, synchronous_mirror: bool = False):
        self.config = config.validate()
        self.recorder = recorder or Recorder()
        self.post = post
        self.timer = timer
        self.lock = threading.Lock()
        self.counter = itertools.count(1)
        self.dropped = 0
        self.synchronous_mirror = synchronous_mirror
        self.jobs: "queue.Queue[Optional[Tuple[Backend, str, str, Any, Optional[int]]]]" = queue.Queue(maxsize=queue_size)
        self.workers = [] if synchronous_mirror else [threading.Thread(target=self.mirror_worker, daemon=True) for _ in range(mirror_workers)]
        for worker in self.workers:
            worker.start()

    def snapshot(self) -> RouteConfig:
        with self.lock:
            return dataclasses.replace(self.config)

    def configure(self, config: RouteConfig) -> None:
        config.validate()
        with self.lock:
            self.config = dataclasses.replace(config)

    def call(self, backend: Backend, body: Any, headers: Dict[str, str]) -> Tuple[int, Any, float]:
        start = self.timer()
        try:
            status, payload = self.post(f"{backend.url}/predict", body, headers)
        except UpstreamError as exc:
            status, payload = 502, {"error": f"{backend.name}: {exc}"}
        return status, payload, (self.timer() - start) * 1000.0

    def route(self, body: Any, headers: Dict[str, str]) -> AppResult:
        config = self.snapshot()
        request_id = headers.get("x-request-id") or f"req-{next(self.counter)}"
        user = headers.get("x-user-id") or (str(body.get("user") or "") if isinstance(body, dict) else "") or request_id
        arm, backend = PRIMARY, config.primary
        if config.canary is not None and assign(user, config.cohort_salt(), config.weight) == CANARY:
            arm, backend = CANARY, config.canary
        status, payload, latency = self.call(backend, body, {"X-Request-Id": request_id})
        ok = status < 500
        label = payload.get("label") if ok and isinstance(payload, dict) else None
        self.recorder.add(Sample(self.recorder.clock(), backend.name, SERVED, round(latency, 3), ok, request_id, user, label))
        if config.shadow is not None and fraction("mirror", request_id) * 100.0 < config.shadow_percent:
            self.mirror((config.shadow, request_id, user, body, label))
        return status, payload, {"X-Model-Version": backend.name, "X-Request-Id": request_id, "X-Route": arm}

    def mirror(self, job: Tuple[Backend, str, str, Any, Optional[int]]) -> None:
        if self.synchronous_mirror:
            self.shadow_call(*job)
            return
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            with self.lock:
                self.dropped += 1

    def shadow_call(self, backend: Backend, request_id: str, user: str, body: Any, served_label: Optional[int]) -> None:
        try:
            status, payload, latency = self.call(backend, body, {"X-Request-Id": request_id, "X-Shadow": "true"})
        except Exception as exc:
            status, payload, latency = 599, {"error": str(exc)}, 0.0
        ok = status < 500
        label = payload.get("label") if ok and isinstance(payload, dict) else None
        agree = (label == served_label) if label is not None and served_label is not None else None
        self.recorder.add(Sample(self.recorder.clock(), backend.name, SHADOW, round(latency, 3), ok, request_id, user, label, agree))

    def mirror_worker(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                if job is None:
                    return
                self.shadow_call(*job)
            finally:
                self.jobs.task_done()

    def drain(self) -> None:
        self.jobs.join()

    def close(self) -> None:
        for _ in self.workers:
            self.jobs.put(None)
        for worker in self.workers:
            worker.join(timeout=10)


def router_app(router: Router) -> App:
    stats = stats_app(router.recorder)

    def app(method: str, path: str, query: Dict[str, str], body: Any, headers: Dict[str, str]) -> AppResult:
        if method == "POST" and path == "/predict":
            return router.route(body, headers)
        if path == "/admin/config":
            if method == "POST":
                router.configure(RouteConfig.from_json(body))
            config = router.snapshot().to_json()
            config["mirror_dropped"] = router.dropped
            return 200, config, {}
        return stats(method, path, query, body, headers)
    return app


class LoadGenerator:
    def __init__(self, url: str, rps: float = 40.0, concurrency: int = 8, users: int = 500, seed: int = 7, recorder: Optional[Recorder] = None,
                 feedback: bool = False, prefix: str = "r", post: Callable[..., Tuple[int, Any, Dict[str, str]]] = http_json,
                 timer: Callable[[], float] = time.perf_counter):
        if rps <= 0 or concurrency <= 0 or users <= 0:
            raise ValueError("rps, concurrency and users must be positive")
        self.url = url.rstrip("/")
        self.rps, self.concurrency, self.users = rps, concurrency, users
        self.examples = make_dataset(seed)
        self.rng = random.Random(seed + 1)
        self.recorder = recorder
        self.feedback = feedback
        self.prefix = prefix
        self.post = post
        self.timer = timer
        self.lock = threading.Lock()
        self.sent = 0
        self.versions: Dict[str, int] = {}

    def one(self, index: int) -> Sample:
        with self.lock:
            example = self.examples[self.rng.randrange(len(self.examples))]
            user = f"u{self.rng.randrange(self.users)}"
        request_id = f"{self.prefix}-{index}"
        start = self.timer()
        try:
            status, body, headers = self.post("POST", f"{self.url}/predict", {"id": example.id, "features": list(example.features), "user": user},
                                              {"X-User-Id": user, "X-Request-Id": request_id})
        except UpstreamError:
            status, body, headers = 599, {}, {}
        latency = (self.timer() - start) * 1000.0
        version = headers.get("x-model-version") or (body.get("version") if isinstance(body, dict) else None) or "unknown"
        ok = status < 500
        label = body.get("label") if ok and isinstance(body, dict) else None
        good = (label == example.label) if label is not None else None
        sample = Sample(time.time() if self.recorder is None else self.recorder.clock(), version, SERVED, round(latency, 3), ok, request_id, user, label,
                        None, good if self.recorder is not None else None)
        with self.lock:
            self.sent += 1
            self.versions[version] = self.versions.get(version, 0) + 1
        if self.recorder is not None:
            self.recorder.add(sample)
        if self.feedback and good is not None:
            try:
                self.post("POST", f"{self.url}/feedback", {"request_id": request_id, "good": good}, {})
            except UpstreamError:
                pass
        return sample

    def run(self, duration: float, stop: threading.Event, report: Callable[[str], None] = print) -> None:
        jobs: "queue.Queue[Optional[int]]" = queue.Queue(maxsize=self.concurrency * 4)

        def worker() -> None:
            while True:
                index = jobs.get()
                if index is None:
                    return
                self.one(index)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(self.concurrency)]
        for thread in threads:
            thread.start()
        interval = 1.0 / self.rps
        started = next_at = last_report = time.monotonic()
        end = started + duration if duration > 0 else math.inf
        index = 0
        while not stop.is_set() and time.monotonic() < end:
            index += 1
            try:
                jobs.put(index, timeout=1.0)
            except queue.Full:
                continue
            next_at += interval
            delay = next_at - time.monotonic()
            if delay > 0:
                stop.wait(delay)
            elif delay < -1.0:
                next_at = time.monotonic()
            if time.monotonic() - last_report >= 5.0:
                last_report = time.monotonic()
                with self.lock:
                    report(f"sent {self.sent} requests, {self.sent / max(last_report - started, 1e-9):.1f} req/s, by version {json.dumps(self.versions, sort_keys=True)}")
        for _ in threads:
            jobs.put(None)
        for thread in threads:
            thread.join(timeout=10)


@dataclass
class Step:
    kind: str
    value: Any = None
    label: str = ""


def number_text(value: float) -> str:
    return f"{value:g}"


def parse_duration(value: Any) -> float:
    if isinstance(value, bool):
        raise PlanError(f"cannot read duration {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)?", str(value).strip())
    if not match:
        raise PlanError(f"cannot read duration {value!r}; use 30s, 2m or a number of seconds")
    return float(match.group(1)) * DURATION_UNITS[match.group(2) or "s"]


def steps_of(doc: Any) -> List[Any]:
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict) and isinstance(doc.get("steps"), list):
        return doc["steps"]
    if isinstance(doc, dict) and doc.get("kind") == "Rollout":
        steps = (((doc.get("spec") or {}).get("strategy") or {}).get("canary") or {}).get("steps")
        if isinstance(steps, list):
            return steps
        raise PlanError("the Rollout has no spec.strategy.canary.steps")
    raise PlanError('a plan is a list of steps, an object with "steps", or an Argo Rollouts Rollout')


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_plan(doc: Any) -> List[Step]:
    raw = steps_of(doc)
    if not raw:
        raise PlanError("the plan has no steps")
    steps = []
    for number, item in enumerate(raw, 1):
        if not isinstance(item, dict) or len(item) != 1:
            raise PlanError(f"step {number}: each step has exactly one key, got {item!r}")
        (kind, value), = item.items()
        if kind == "setWeight":
            if not is_number(value) or not 0 <= value <= 100:
                raise PlanError(f"step {number}: setWeight must be a number from 0 to 100, got {value!r}")
            steps.append(Step(kind, float(value), f"setWeight {number_text(value)}"))
        elif kind == "setMirrorRoute":
            if not isinstance(value, dict) or not value.get("name"):
                raise PlanError(f"step {number}: setMirrorRoute needs a name")
            percentage = value.get("percentage")
            if percentage is None:
                steps.append(Step(kind, None, f"setMirrorRoute {value['name']}: removed"))
            elif not is_number(percentage) or not 0 < percentage <= 100:
                raise PlanError(f"step {number}: setMirrorRoute percentage must be above 0 and at most 100, got {percentage!r}")
            else:
                steps.append(Step(kind, float(percentage), f"setMirrorRoute {value['name']}: {number_text(percentage)}%"))
        elif kind == "pause":
            duration = value.get("duration") if isinstance(value, dict) else None
            if duration is None:
                raise PlanError(f"step {number}: minirollout needs pause.duration; an open-ended pause waits for a human (kubectl argo rollouts promote)")
            try:
                seconds = parse_duration(duration)
            except PlanError as exc:
                raise PlanError(f"step {number}: {exc}") from None
            steps.append(Step(kind, seconds, f"pause {duration}"))
        elif kind == "analysis":
            templates = [t.get("templateName") for t in (value or {}).get("templates", []) if isinstance(t, dict)] if isinstance(value, dict) else []
            steps.append(Step(kind, value, "analysis" + (f" ({', '.join(str(t) for t in templates)})" if templates else "")))
        else:
            raise PlanError(f"step {number}: minirollout does not implement {kind}; it knows setMirrorRoute, setWeight, pause and analysis")
    return steps


def describe_plan(steps: Sequence[Step], rps: float, th: Thresholds, baseline: float, window: float) -> Dict[str, Any]:
    needed = samples_needed(baseline, th.quality_drop, th.alpha)
    rows, warnings = [], []
    weight = mirror = 0.0
    for index, step in enumerate(steps, 1):
        if step.kind == "setWeight":
            weight = step.value
        elif step.kind == "setMirrorRoute":
            mirror = step.value or 0.0
        row: Dict[str, Any] = {"step": index, "action": step.label, "live_percent": weight, "mirror_percent": mirror}
        if step.kind == "analysis":
            mode = SHADOW if mirror > 0 and weight == 0 else SERVED
            rate = rps * (mirror if mode == SHADOW else weight) / 100.0
            seconds = th.min_samples / rate if rate > 0 else None
            row.update(mode=mode, candidate_rps=round(rate, 3), seconds_to_min_samples=round(seconds, 1) if seconds else None,
                       users_exposed=0 if mode == SHADOW else f"~{number_text(weight)}%")
            if rate <= 0:
                warnings.append(f"step {index}: the analysis compares a candidate that receives no traffic and will hold until it times out")
            elif rate * window < th.min_samples:
                warnings.append(f"step {index}: a {number_text(window)}s window holds about {rate * window:.0f} candidate samples at {rate:.1f}/s, fewer than min-samples {th.min_samples}; the analysis can never decide")
        rows.append(row)
    if not any(step.kind == "analysis" for step in steps):
        warnings.append("no analysis step: the candidate is promoted without ever being compared with the primary")
    if th.min_samples < needed:
        warnings.append(f"min-samples {th.min_samples} is below the {needed} per side needed to detect a quality drop of {number_text(th.quality_drop)} "
                        f"from {number_text(baseline)} at alpha {number_text(th.alpha)} with 80% power")
    return {"samples_needed": needed, "rows": rows, "warnings": warnings}


class RouterClient:
    def __init__(self, url: str, request: Callable[..., Tuple[int, Any, Dict[str, str]]] = http_json):
        self.url = url.rstrip("/")
        self.request = request

    def config(self) -> RouteConfig:
        status, body, _ = self.request("GET", f"{self.url}/admin/config")
        if status != 200:
            raise UpstreamError(f"GET {self.url}/admin/config: HTTP {status}: {body}")
        return RouteConfig.from_json(body)

    def configure(self, config: RouteConfig) -> None:
        status, body, _ = self.request("POST", f"{self.url}/admin/config", config.to_json())
        if status != 200:
            raise UpstreamError(f"POST {self.url}/admin/config: HTTP {status}: {body}")

    def samples(self, version: Optional[str], role: Optional[str], since: Optional[float], until: Optional[float]) -> List[Sample]:
        return fetch_samples(self.url, version, role, since, until, self.request)


class LocalClient:
    def __init__(self, router: Router):
        self.router = router

    def config(self) -> RouteConfig:
        return self.router.snapshot()

    def configure(self, config: RouteConfig) -> None:
        self.router.configure(config)

    def samples(self, version: Optional[str], role: Optional[str], since: Optional[float], until: Optional[float]) -> List[Sample]:
        return self.router.recorder.select(version, role, since, until)


def fetch_samples(url: str, version: Optional[str] = None, role: Optional[str] = None, since: Optional[float] = None, until: Optional[float] = None,
                  request: Callable[..., Tuple[int, Any, Dict[str, str]]] = http_json) -> List[Sample]:
    params = {k: v for k, v in (("version", version), ("role", role), ("since", None if since is None else repr(since)), ("until", None if until is None else repr(until))) if v is not None}
    status, body, _ = request("GET", f"{url.rstrip('/')}/samples" + (f"?{urllib.parse.urlencode(params)}" if params else ""))
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("samples"), list):
        raise UpstreamError(f"GET {url}/samples: HTTP {status}: {str(body)[:200]}")
    return [Sample.from_json(s) for s in body["samples"]]


class FileRegistry:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> Dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {"models": {}}
        except ValueError as exc:
            raise RegistryError(f"{self.path} is not JSON: {exc}") from None
        if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
            raise RegistryError(f'{self.path} must look like {{"models": {{...}}}}')
        return data

    def save(self, data: Dict[str, Any]) -> None:
        temporary = f"{self.path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        os.replace(temporary, self.path)

    def entry(self, data: Dict[str, Any], model: str) -> Dict[str, Any]:
        return data["models"].setdefault(model, {"aliases": {}, "versions": {}})

    def set_alias(self, model: str, alias: str, version: str) -> None:
        data = self.load()
        entry = self.entry(data, model)
        entry["aliases"][alias] = str(version)
        entry["versions"].setdefault(str(version), {"tags": {}})
        self.save(data)

    def get_alias(self, model: str, alias: str) -> Optional[str]:
        return self.load()["models"].get(model, {}).get("aliases", {}).get(alias)

    def set_tag(self, model: str, version: str, key: str, value: str) -> None:
        data = self.load()
        self.entry(data, model)["versions"].setdefault(str(version), {"tags": {}})["tags"][key] = value
        self.save(data)


class MlflowRegistry:
    def __init__(self, url: str, request: Callable[..., Tuple[int, Any, Dict[str, str]]] = http_json):
        self.url = url.rstrip("/")
        self.request = request

    def call(self, method: str, path: str, payload: Any = None, query: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
        url = f"{self.url}/api/2.0/mlflow/{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
        try:
            status, body, _ = self.request(method, url, payload)
        except UpstreamError as exc:
            raise RegistryError(str(exc)) from None
        return status, body

    def checked(self, method: str, path: str, payload: Any = None, query: Optional[Dict[str, str]] = None) -> Any:
        status, body = self.call(method, path, payload, query)
        if status != 200:
            detail = f"{body.get('error_code', '')} {body.get('message', '')}".strip() if isinstance(body, dict) else str(body)
            raise RegistryError(f"{method} {path}: HTTP {status} {detail}".strip())
        return body

    def set_alias(self, model: str, alias: str, version: str) -> None:
        self.checked("POST", "registered-models/alias", {"name": model, "alias": alias, "version": str(version)})

    def get_alias(self, model: str, alias: str) -> Optional[str]:
        status, body = self.call("GET", "registered-models/alias", query={"name": model, "alias": alias})
        if status == 404:
            return None
        if status != 200:
            raise RegistryError(f"GET registered-models/alias: HTTP {status} {body}")
        return str(body["model_version"]["version"])

    def set_tag(self, model: str, version: str, key: str, value: str) -> None:
        self.checked("POST", "model-versions/set-tag", {"name": model, "version": str(version), "key": key, "value": value})


def registry_from(target: Optional[str]) -> Any:
    if not target:
        return None
    if target.startswith(("http://", "https://")):
        return MlflowRegistry(target)
    return FileRegistry(target)


def registry_version_of(name: str) -> str:
    match = re.search(r"(\d+)$", name)
    if not match:
        raise PlanError(f"cannot derive a registry version number from {name!r}; pass --registry-version")
    return str(int(match.group(1)))


class Controller:
    def __init__(self, steps: Sequence[Step], client: Any, candidate: Backend, thresholds: Optional[Thresholds] = None, window: float = 20.0,
                 tick: float = 1.0, analysis_timeout: float = 120.0, registry: Any = None, model: str = "model", registry_version: str = "",
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print,
                 state_path: Optional[str] = None):
        self.steps = list(steps)
        self.client = client
        self.candidate = candidate
        self.thresholds = thresholds or Thresholds()
        self.window, self.tick, self.analysis_timeout = window, tick, analysis_timeout
        self.registry, self.model, self.registry_version = registry, model, registry_version
        self.clock, self.sleep, self.log = clock, sleep, log
        self.state_path = state_path
        self.state: Dict[str, Any] = {}
        self.primary = candidate
        self.started = 0.0

    def save(self) -> None:
        if self.state_path:
            with open(self.state_path, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, indent=2)

    def event(self, message: str) -> None:
        self.state["events"].append({"t": round(self.clock(), 3), "message": message})
        self.log(message)
        self.save()

    def run(self) -> Dict[str, Any]:
        config = self.client.config()
        self.primary = config.primary
        if self.primary.name == self.candidate.name:
            raise PlanError(f"{self.candidate.name} is already the primary")
        self.started = self.clock()
        self.state = {"model": self.model, "primary": self.primary.name, "candidate": self.candidate.name, "phase": PROGRESSING, "step": None,
                      "steps": [s.label for s in self.steps], "analyses": [], "events": [], "exposure": {}}
        self.event(f"rollout {self.candidate.name} over {self.primary.name}: {len(self.steps)} steps")
        for index, step in enumerate(self.steps):
            self.state["step"] = index
            self.event(f"step {index + 1}/{len(self.steps)}: {step.label}")
            outcome = self.run_step(index, step)
            if outcome == ROLLBACK:
                self.rollback(index, step)
                return self.state
            if outcome == HOLD:
                self.hold(index)
                return self.state
        self.promote()
        return self.state

    def run_step(self, index: int, step: Step) -> str:
        if step.kind == "setWeight":
            config = self.client.config()
            config.canary, config.weight = (self.candidate if step.value > 0 else None), step.value
            self.client.configure(config)
            return PROMOTE
        if step.kind == "setMirrorRoute":
            config = self.client.config()
            config.shadow, config.shadow_percent = (None, 0.0) if step.value is None else (self.candidate, step.value)
            self.client.configure(config)
            return PROMOTE
        if step.kind == "pause":
            deadline = self.clock() + step.value
            while self.clock() < deadline:
                self.sleep(min(self.tick, max(deadline - self.clock(), 0.0)))
            return PROMOTE
        return self.analyse(index)

    def analyse(self, index: int) -> str:
        config = self.client.config()
        mode = SHADOW if config.shadow is not None and config.shadow.name == self.candidate.name and config.weight == 0 else SERVED
        started = self.clock()
        last_decision, last_logged = "", -math.inf
        while True:
            now = self.clock()
            since = now - self.window
            result = analyze(self.client.samples(self.primary.name, SERVED, since, now), self.client.samples(self.candidate.name, mode, since, now), self.thresholds, mode)
            finished = result.decision != HOLD or now - started >= self.analysis_timeout
            if finished or result.decision != last_decision or now - last_logged >= 5.0:
                summary = "; ".join(result.reasons()) or "every check passed"
                self.log(f"  analysis step {index + 1} ({mode}): {result.decision}: {summary}")
                last_decision, last_logged = result.decision, now
            if finished:
                self.state["analyses"].append(dict(result.to_json(), step=index, t=round(now, 3)))
                self.save()
                return result.decision
            self.sleep(self.tick)

    def record_exposure(self) -> None:
        now = self.clock()
        served = self.client.samples(self.candidate.name, SERVED, self.started, now)
        shadowed = self.client.samples(self.candidate.name, SHADOW, self.started, now)
        self.state["exposure"] = {"served_requests": len(served), "users": len({s.user for s in served}), "shadow_requests": len(shadowed)}

    def tag(self, value: str) -> None:
        if self.registry is not None and self.registry_version:
            self.registry.set_tag(self.model, self.registry_version, "rollout", value)

    def promote(self) -> None:
        config = self.client.config()
        self.client.configure(RouteConfig(primary=self.candidate, salt=config.salt))
        self.record_exposure()
        self.state["phase"] = PROMOTED
        self.event(f"promoted {self.candidate.name}: it is now the primary")
        if self.registry is not None:
            self.registry.set_alias(self.model, CHAMPION, self.registry_version)
            self.tag("promoted")
            self.event(f"registry: {self.model}@{CHAMPION} -> version {self.registry_version}")

    def rollback(self, index: int, step: Step) -> None:
        config = self.client.config()
        self.client.configure(RouteConfig(primary=self.primary, salt=config.salt))
        self.record_exposure()
        reasons = self.state["analyses"][-1]["reasons"] if self.state["analyses"] else []
        self.state["phase"] = ROLLED_BACK
        self.state["failed_step"] = index
        exposure = self.state["exposure"]
        self.event(f"rolled back {self.candidate.name} at step {index + 1} ({step.label}): {'; '.join(reasons)}; "
                   f"it served {exposure['served_requests']} requests to {exposure['users']} users")
        self.tag("rolled-back")

    def hold(self, index: int) -> None:
        self.record_exposure()
        self.state["phase"] = HELD
        self.event(f"analysis step {index + 1} reached no decision in {number_text(self.analysis_timeout)}s; traffic stays as it is until a human promotes or rolls back")


def replay(queries: Sequence[Dict[str, Any]], samples: Sequence[Sample], th: Thresholds) -> List[Dict[str, Any]]:
    rows = []
    for query in queries:
        role = query.get("role") or SERVED
        primary = [s for s in samples if in_window(s, query["primary"], SERVED, query["since"], query["until"])]
        candidate = [s for s in samples if in_window(s, query["candidate"], role, query["since"], query["until"])]
        result = analyze(primary, candidate, th, role)
        rows.append({"step": str(query.get("step", "")), "since": query["since"], "until": query["until"], "primary": query["primary"],
                     "candidate": query["candidate"], "role": role, "decision": result.decision, "reasons": result.reasons(),
                     "candidate_samples": len(candidate), "primary_samples": len(primary)})
    return rows


def load_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise PlanError(f"cannot read {path}: {exc.strerror}") from None
    except ValueError as exc:
        raise PlanError(f"{path} is not JSON: {exc}") from None


def thresholds_from(args: argparse.Namespace) -> Thresholds:
    th = Thresholds(args.alpha, args.min_samples, args.max_latency_ms, args.max_error_rate, args.max_quality_drop, args.max_disagreement)
    if not 0 < th.alpha < 1 or th.min_samples < 1 or not 0 < th.disagreement < 1:
        raise PlanError("alpha and max-disagreement must be between 0 and 1, min-samples at least 1")
    return th


def print_analysis(result: Analysis) -> None:
    print(f"{'metric':<13} {'test':<17} {'n primary':>9} {'n cand.':>8} {'primary':>10} {'candidate':>10} {'effect':>10} {'limit':>7} {'p':>9}  verdict")
    for c in result.checks:
        print(f"{c.metric:<13} {c.test:<17} {c.n_primary:>9} {c.n_candidate:>8} {c.primary:>10.4g} {c.candidate:>10.4g} {c.effect:>+10.4g} {c.limit:>7g} {c.p_value:>9.2g}  {c.verdict}")
    print(f"decision: {result.decision} ({result.mode})")


def listen_address(text: str) -> Tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        raise PlanError(f"--listen must be HOST:PORT, got {text!r}")
    return host or "127.0.0.1", int(port)


def serve_until_interrupted(server: ThreadingHTTPServer, banner: str) -> int:
    print(f"{banner} listening on {server_url(server)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_model(args: argparse.Namespace) -> int:
    model = ModelBehaviour(args.version, parse_weights(args.weights), args.latency_ms, args.jitter_ms, args.error_rate)
    return serve_until_interrupted(make_server(model_app(model), args.host, args.port), f"model {model.version}")


def cmd_router(args: argparse.Namespace) -> int:
    router = Router(RouteConfig(Backend.parse(args.primary)), mirror_workers=args.mirror_workers)
    host, port = listen_address(args.listen)
    return serve_until_interrupted(make_server(router_app(router), host, port), "router")


def cmd_load(args: argparse.Namespace) -> int:
    recorder = Recorder() if args.record else None
    generator = LoadGenerator(args.url, args.rps, args.concurrency, args.users, args.seed, recorder, args.feedback, args.prefix)
    if args.listen:
        if recorder is None:
            raise PlanError("--listen serves what --record collected; pass --record too")
        host, port = listen_address(args.listen)
        server = start_server(stats_app(recorder), host, port)
        print(f"load stats listening on {server_url(server)}", flush=True)
    print(f"sending {args.rps:g} req/s to {args.url}", flush=True)
    stop = threading.Event()
    try:
        generator.run(args.duration, stop, report=lambda line: print(line, flush=True))
        if args.listen:
            stop.wait()
    except KeyboardInterrupt:
        stop.set()
    return 0


def cmd_assign(args: argparse.Namespace) -> int:
    users = args.user or [f"{args.prefix}{i}" for i in range(args.users)]
    canary = [u for u in users if assign(u, args.salt, args.weight) == CANARY]
    result = {"weight": args.weight, "salt": args.salt, "users": len(users), "canary": len(canary), "share": round(len(canary) / len(users), 6) if users else 0.0,
              "canary_users": canary if args.user else canary[:20]}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{len(canary)} of {len(users)} users ({100 * result['share']:.1f}%) are in the canary cohort at weight {args.weight:g} with salt {args.salt!r}")
        for user in users[:20] if not args.user else users:
            print(f"  {user:<12} bucket {bucket(user, args.salt):7.3f}  {assign(user, args.salt, args.weight)}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    steps = parse_plan(load_json(args.plan))
    report = describe_plan(steps, args.rps, thresholds_from(args), args.baseline_quality, args.window)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"{'#':<3} {'step':<34} {'live %':>7} {'mirror %':>9} {'mode':>7} {'cand. req/s':>12} {'s to n':>7}")
    for row in report["rows"]:
        seconds = row.get("seconds_to_min_samples")
        print(f"{row['step']:<3} {row['action']:<34} {row['live_percent']:>7g} {row['mirror_percent']:>9g} {row.get('mode', ''):>7} "
              f"{row.get('candidate_rps', ''):>12} {'' if seconds is None else seconds:>7}")
    print(f"samples per side to detect the quality drop: {report['samples_needed']}")
    for warning in report["warnings"]:
        print(f"warning: {warning}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    th = thresholds_from(args)
    if args.samples:
        doc = load_json(args.samples)
        samples = [Sample.from_json(s) for s in (doc.get("samples") if isinstance(doc, dict) else doc) or []]
        until = args.until if args.until is not None else max((s.t for s in samples), default=0.0)
    else:
        until = args.until if args.until is not None else time.time()
        samples = fetch_samples(args.source, None, None, until - args.window if args.since is None else args.since, until)
    since = args.since if args.since is not None else until - args.window
    primary = [s for s in samples if in_window(s, args.primary, SERVED, since, until)]
    candidate = [s for s in samples if in_window(s, args.candidate, args.role, since, until)]
    result = analyze(primary, candidate, th, args.role)
    if args.json:
        print(json.dumps(dict(result.to_json(), since=since, until=until), indent=2))
    else:
        print_analysis(result)
    return EXIT_CODES[result.decision]


def cmd_replay(args: argparse.Namespace) -> int:
    status, body, _ = http_json("GET", f"{args.source.rstrip('/')}/queries")
    if status != 200 or not isinstance(body, dict):
        raise UpstreamError(f"GET {args.source}/queries: HTTP {status}")
    queries = [q for q in body.get("queries", []) if args.step is None or str(q.get("step")) == args.step]
    if not queries:
        raise PlanError(f"{args.source} has logged no /compare queries" + (f" for step {args.step}" if args.step is not None else ""))
    samples = fetch_samples(args.source, None, None, min(q["since"] for q in queries), max(q["until"] for q in queries))
    rows = replay(queries, samples, thresholds_from(args))
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            print(f"step {row['step']:<3} {row['candidate']} vs {row['primary']} ({row['candidate_samples']} vs {row['primary_samples']} samples): "
                  f"{row['decision']}" + (f": {'; '.join(row['reasons'])}" if row["reasons"] else ""))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    steps = parse_plan(load_json(args.plan))
    candidate = Backend.parse(args.candidate)
    registry = registry_from(args.registry)
    version = args.registry_version or (registry_version_of(candidate.name) if registry is not None else "")
    controller = Controller(steps, RouterClient(args.router), candidate, thresholds_from(args), args.window, args.tick, args.analysis_timeout,
                            registry, args.model, version, state_path=args.state, log=lambda line: print(line, flush=True))
    try:
        state = controller.run()
    except RegistryError as exc:
        print(f"minirollout: the rollout finished as {controller.state.get('phase')} but the registry was not updated: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(state, indent=2))
    return PHASE_EXIT[state["phase"]]


def add_threshold_args(parser: argparse.ArgumentParser) -> None:
    defaults = Thresholds()
    parser.add_argument("--alpha", type=float, default=defaults.alpha, help="significance level of every test")
    parser.add_argument("--min-samples", type=int, default=defaults.min_samples, help="samples per side before any verdict")
    parser.add_argument("--max-latency-ms", type=float, default=defaults.latency_ms, help="largest tolerated rise of the median latency")
    parser.add_argument("--max-error-rate", type=float, default=defaults.error_rate, help="largest tolerated rise of the error rate")
    parser.add_argument("--max-quality-drop", type=float, default=defaults.quality_drop, help="largest tolerated drop of the positive feedback rate")
    parser.add_argument("--max-disagreement", type=float, default=defaults.disagreement, help="largest tolerated shadow disagreement with the primary")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minirollout", description="shadow, canary and compare model versions behind one router")
    sub = parser.add_subparsers(dest="command", required=True)

    model = sub.add_parser("model", help="a stub model server: a logistic regression with fixed weights, injected latency and errors")
    model.add_argument("--version", default=os.environ.get("MODEL_VERSION", "v1"))
    model.add_argument("--weights", default=os.environ.get("MODEL_WEIGHTS", ",".join(str(w) for w in TRUE_WEIGHTS)))
    model.add_argument("--latency-ms", type=float, default=float(os.environ.get("LATENCY_MS", "5")))
    model.add_argument("--jitter-ms", type=float, default=float(os.environ.get("JITTER_MS", "5")))
    model.add_argument("--error-rate", type=float, default=float(os.environ.get("ERROR_RATE", "0")))
    model.add_argument("--host", default="127.0.0.1")
    model.add_argument("--port", type=int, default=0)
    model.set_defaults(func=cmd_model)

    router = sub.add_parser("router", help="route to a primary, mirror to a shadow, split a sticky canary; serve /admin/config, /compare and /samples")
    router.add_argument("--primary", required=True, help="NAME=URL of the primary version")
    router.add_argument("--listen", default="127.0.0.1:0")
    router.add_argument("--mirror-workers", type=int, default=2)
    router.set_defaults(func=cmd_router)

    load = sub.add_parser("load", help="send labelled requests; post feedback to a router or record per-version samples and serve /compare")
    load.add_argument("--url", required=True)
    load.add_argument("--rps", type=float, default=40.0)
    load.add_argument("--concurrency", type=int, default=8)
    load.add_argument("--users", type=int, default=500)
    load.add_argument("--seed", type=int, default=7)
    load.add_argument("--duration", type=float, default=0.0, help="seconds; 0 runs until interrupted")
    load.add_argument("--prefix", default="r", help="request id prefix")
    load.add_argument("--feedback", action="store_true", help="POST /feedback to the target after each answer")
    load.add_argument("--record", action="store_true", help="record samples here, with the label as feedback")
    load.add_argument("--listen", default="", help="HOST:PORT for /compare, /stats, /samples and /queries (needs --record)")
    load.set_defaults(func=cmd_load)

    run = sub.add_parser("run", help="walk a plan against a router: exit 0 promoted, 1 rolled back, 3 held")
    run.add_argument("plan", help="JSON plan: a list of steps, {\"steps\": [...]}, or an Argo Rollouts Rollout")
    run.add_argument("--router", required=True)
    run.add_argument("--candidate", required=True, help="NAME=URL of the new version")
    run.add_argument("--window", type=float, default=20.0, help="seconds of samples each analysis looks at")
    run.add_argument("--tick", type=float, default=1.0)
    run.add_argument("--analysis-timeout", type=float, default=120.0)
    run.add_argument("--registry", default="", help="a JSON file or an MLflow tracking server URL")
    run.add_argument("--model", default="model", help="registered model name")
    run.add_argument("--registry-version", default="", help="registry version of the candidate (default: the digits at the end of its name)")
    run.add_argument("--state", default="")
    run.add_argument("--json", action="store_true")
    add_threshold_args(run)
    run.set_defaults(func=cmd_run)

    analyze_parser = sub.add_parser("analyze", help="compare a candidate with the primary over one window: exit 0 promote, 1 rollback, 3 hold")
    source = analyze_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", help="URL of a router or load generator")
    source.add_argument("--samples", help="JSON file with samples")
    analyze_parser.add_argument("--primary", required=True)
    analyze_parser.add_argument("--candidate", required=True)
    analyze_parser.add_argument("--role", choices=(SERVED, SHADOW), default=SERVED)
    analyze_parser.add_argument("--window", type=float, default=20.0)
    analyze_parser.add_argument("--since", type=float)
    analyze_parser.add_argument("--until", type=float)
    analyze_parser.add_argument("--json", action="store_true")
    add_threshold_args(analyze_parser)
    analyze_parser.set_defaults(func=cmd_analyze)

    replay_parser = sub.add_parser("replay", help="re-analyse every window a /compare caller (Argo Rollouts) looked at")
    replay_parser.add_argument("--source", required=True)
    replay_parser.add_argument("--step")
    replay_parser.add_argument("--json", action="store_true")
    add_threshold_args(replay_parser)
    replay_parser.set_defaults(func=cmd_replay)

    plan = sub.add_parser("plan", help="print a plan with the traffic each analysis gets and how long it needs")
    plan.add_argument("plan")
    plan.add_argument("--rps", type=float, default=40.0)
    plan.add_argument("--baseline-quality", type=float, default=0.95)
    plan.add_argument("--window", type=float, default=20.0)
    plan.add_argument("--json", action="store_true")
    add_threshold_args(plan)
    plan.set_defaults(func=cmd_plan)

    assign_parser = sub.add_parser("assign", help="show which users a weight and salt put in the canary cohort")
    assign_parser.add_argument("user", nargs="*")
    assign_parser.add_argument("--weight", type=float, required=True)
    assign_parser.add_argument("--salt", default="")
    assign_parser.add_argument("--users", type=int, default=1000)
    assign_parser.add_argument("--prefix", default="u")
    assign_parser.add_argument("--json", action="store_true")
    assign_parser.set_defaults(func=cmd_assign)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (PlanError, ValueError) as exc:
        print(f"minirollout: {exc}", file=sys.stderr)
        return 2
    except (UpstreamError, RegistryError) as exc:
        print(f"minirollout: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
