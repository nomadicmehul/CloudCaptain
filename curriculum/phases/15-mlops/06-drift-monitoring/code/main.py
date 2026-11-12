from __future__ import annotations

import argparse
import csv
import http.server
import json
import math
import os
import random
import re
import sys
import threading
import time
from bisect import bisect_right
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

MISSING_TOKENS = {"", "na", "nan", "null", "none", "n/a"}
TOKEN = re.compile(r"[a-z0-9']+")
P_VALUE_METHODS = {"ks", "chisquare"}
DEFAULT_THRESHOLDS = {"psi": 0.1, "ks": 0.05, "wasserstein": 0.1, "jensenshannon": 0.1, "chisquare": 0.05}
NUMERICAL_METHODS = ("psi", "ks", "wasserstein", "jensenshannon")
CATEGORICAL_METHODS = ("psi", "chisquare", "jensenshannon")
SMALL_SAMPLE = 1000
PER_VALUE_LIMIT = 20
EXACT_KS_LIMIT = 10000
TEXT_MIN_TOKENS = 4.0
NUMERIC_SHARE = 0.95


class InputError(Exception):
    pass


@dataclass
class Options:
    model: str = "model"
    num_method: Optional[str] = None
    cat_method: Optional[str] = None
    thresholds: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    drift_share: float = 0.5
    max_missing_increase: float = 0.05
    max_out_of_range: float = 0.01
    oov_threshold: float = 0.05
    decay_threshold: float = 0.05
    min_label_coverage: float = 0.2


@dataclass
class ColumnData:
    values: List[Any]
    rows: int
    missing: int = 0
    type_errors: int = 0


@dataclass
class ColumnResult:
    column: str
    kind: str
    role: str
    method: str
    score: float
    threshold: float
    drifted: bool
    stats: Dict[str, Any]


def is_missing(raw: Optional[str]) -> bool:
    return raw is None or raw.strip().lower() in MISSING_TOKENS


def parse_float(raw: str) -> Optional[float]:
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def fsum_mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values)


def population_std(values: Sequence[float]) -> float:
    mean = fsum_mean(values)
    return math.sqrt(math.fsum((v - mean) ** 2 for v in values) / len(values))


def sturges_edges(pooled: Sequence[float]) -> List[float]:
    lo, hi = min(pooled), max(pooled)
    width = (hi - lo) / (math.log2(len(pooled)) + 1.0)
    if lo == hi:
        lo, hi = lo - 0.5, hi + 0.5
    count = math.ceil((hi - lo) / width) if width else 1
    step = (hi - lo) / count
    return [i * step + lo for i in range(count)] + [hi]


def histogram(values: Sequence[float], edges: Sequence[float]) -> List[int]:
    counts = [0] * (len(edges) - 1)
    last = len(edges) - 1
    for value in values:
        if value < edges[0] or value > edges[-1]:
            continue
        index = bisect_right(edges, value) - 1
        counts[min(index, last - 1)] += 1
    return counts


def binned_shares(reference: Sequence[Any], current: Sequence[Any], numerical: bool) -> Tuple[List[float], List[float], List[Any]]:
    if numerical and len(set(reference)) > PER_VALUE_LIMIT:
        edges = sturges_edges(list(reference) + list(current))
        ref_counts, cur_counts = histogram(reference, edges), histogram(current, edges)
        keys: List[Any] = edges[:-1]
    else:
        keys = sorted(set(reference) | set(current), key=lambda k: (str(type(k)), k))
        ref_counter, cur_counter = Counter(reference), Counter(current)
        ref_counts = [ref_counter[k] for k in keys]
        cur_counts = [cur_counter[k] for k in keys]
    return [c / len(reference) for c in ref_counts], [c / len(current) for c in cur_counts], keys


def fill_zeroes(shares: Sequence[float]) -> List[float]:
    smallest = min((s for s in shares if s > 0), default=1.0)
    filler = smallest / 10 ** 6 if smallest <= 0.0001 else 0.0001
    return [s if s > 0 else filler for s in shares]


def psi(reference: Sequence[Any], current: Sequence[Any], numerical: bool = True) -> float:
    ref, cur, _ = binned_shares(reference, current, numerical)
    ref, cur = fill_zeroes(ref), fill_zeroes(cur)
    return math.fsum((r - c) * math.log(r / c) for r, c in zip(ref, cur))


def rel_entr(x: float, y: float) -> float:
    if x > 0 and y > 0:
        return x * math.log(x / y)
    if x == 0 and y >= 0:
        return 0.0
    return math.inf


def jensen_shannon(p: Sequence[float], q: Sequence[float], base: Optional[float] = None) -> float:
    p_total, q_total = math.fsum(p), math.fsum(q)
    p = [v / p_total for v in p]
    q = [v / q_total for v in q]
    m = [(a + b) / 2.0 for a, b in zip(p, q)]
    js = math.fsum(rel_entr(a, c) for a, c in zip(p, m)) + math.fsum(rel_entr(b, c) for b, c in zip(q, m))
    if base is not None:
        js /= math.log(base)
    return math.sqrt(max(js, 0.0) / 2.0)


def js_distance(reference: Sequence[Any], current: Sequence[Any], numerical: bool = True) -> float:
    ref, cur, _ = binned_shares(reference, current, numerical)
    return jensen_shannon(ref, cur)


def wasserstein(u: Sequence[float], v: Sequence[float]) -> float:
    u_sorted, v_sorted = sorted(u), sorted(v)
    pooled = sorted(list(u) + list(v))
    total = []
    for left, right in zip(pooled, pooled[1:]):
        u_cdf = bisect_right(u_sorted, left) / len(u_sorted)
        v_cdf = bisect_right(v_sorted, left) / len(v_sorted)
        total.append(abs(u_cdf - v_cdf) * (right - left))
    return math.fsum(total)


def wasserstein_norm(reference: Sequence[float], current: Sequence[float]) -> float:
    return wasserstein(reference, current) / max(population_std(reference), 0.001)


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def ks_outside_square(n: int, h: int) -> float:
    p = 0.0
    k = n // h
    while k >= 0:
        p1 = 1.0
        for j in range(h):
            p1 = (n - k * h - j) * p1 / (n + k * h + j + 1)
        p = p1 * (1.0 - p)
        k -= 1
    return 2 * p


def ks_outside_lattice(m: int, n: int, g: int, h: int) -> float:
    if m < n:
        m, n = n, m
    mg, ng = m // g, n // g
    minj, maxj = 0, min(ceil_div(h, mg), n + 1)
    curlen = maxj - minj
    size = min(2 * maxj + 2, n + 1)
    paths = [1.0] * size
    for index in range(minj, maxj):
        paths[index] = 0.0
    for i in range(1, m + 1):
        lastminj, lastlen = minj, curlen
        minj = min(max((ng * i - h) // mg + 1, 0), n)
        maxj = min(ceil_div(ng * i + h, mg), n + 1)
        if maxj <= minj:
            return 1.0
        value = 0.0 if minj == 0 else 1.0
        for jj in range(maxj - minj):
            j = jj + minj
            value = (paths[jj + minj - lastminj] * i + value * j) / (i + j)
            paths[jj] = value
        curlen = maxj - minj
        for index in range(curlen, min(lastlen, size)):
            paths[index] = 1.0
    return paths[maxj - minj - 1]


def kolmogorov_sf(lam: float) -> float:
    if lam < 0.2:
        return 1.0
    total = 0.0
    for k in range(1, 101):
        term = 2 * (-1) ** (k - 1) * math.exp(-2 * k * k * lam * lam)
        total += term
        if abs(term) < 1e-16:
            break
    return min(max(total, 0.0), 1.0)


def ks_2samp(reference: Sequence[float], current: Sequence[float]) -> Tuple[float, float, str]:
    a, b = sorted(reference), sorted(current)
    n1, n2 = len(a), len(b)
    if not n1 or not n2:
        raise InputError("the KS test needs at least one value in each window")
    g = math.gcd(n1, n2)
    lcm = n1 // g * n2
    s1, s2 = lcm // n1, lcm // n2
    i = j = best = 0
    while i < n1 and j < n2:
        v = min(a[i], b[j])
        while i < n1 and a[i] == v:
            i += 1
        while j < n2 and b[j] == v:
            j += 1
        best = max(best, abs(i * s1 - j * s2))
    statistic = best / lcm
    if best == 0:
        return statistic, 1.0, "exact"
    if max(n1, n2) <= EXACT_KS_LIMIT:
        p = ks_outside_square(n1, best) if n1 == n2 else ks_outside_lattice(n1, n2, g, best)
        return statistic, min(max(p, 0.0), 1.0), "exact"
    en = n1 * n2 / (n1 + n2)
    lam = (math.sqrt(en) + 0.12 + 0.11 / math.sqrt(en)) * statistic
    return statistic, kolmogorov_sf(lam), "asymptotic"


def gamma_q(a: float, x: float) -> float:
    if x <= 0:
        return 1.0
    if math.isinf(x):
        return 0.0
    log_prefix = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1:
        term = total = 1.0 / a
        ap = a
        for _ in range(10000):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-16:
                break
        return 1.0 - total * math.exp(log_prefix)
    tiny = 1e-300
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b
    h = d
    for i in range(1, 10000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-16:
            break
    return math.exp(log_prefix) * h


def chi2_sf(x: float, df: int) -> float:
    return min(max(gamma_q(df / 2.0, x / 2.0), 0.0), 1.0)


def chi_square(reference: Sequence[str], current: Sequence[str]) -> Tuple[float, float, int]:
    keys = sorted(set(reference) | set(current), key=str)
    ref_counter, cur_counter = Counter(reference), Counter(current)
    scale = len(current) / len(reference)
    statistic = 0.0
    for key in keys:
        expected = ref_counter[key] * scale
        observed = cur_counter[key]
        if expected == 0:
            statistic = math.inf
            break
        statistic += (observed - expected) ** 2 / expected
    df = max(len(keys) - 1, 1)
    return statistic, chi2_sf(statistic, df), df


def choose_method(kind: str, reference_rows: int, override: Optional[str]) -> str:
    if override:
        return override
    if kind == "categorical":
        return "chisquare" if reference_rows <= SMALL_SAMPLE else "jensenshannon"
    return "ks" if reference_rows <= SMALL_SAMPLE else "wasserstein"


def is_drift(method: str, score: float, threshold: float) -> bool:
    return score < threshold if method in P_VALUE_METHODS else score >= threshold


def numerical_stats(reference: List[float], current: List[float]) -> Dict[str, Any]:
    statistic, p_value, mode = ks_2samp(reference, current)
    return {
        "psi": psi(reference, current, True),
        "ks_statistic": statistic,
        "ks_p_value": p_value,
        "ks_mode": mode,
        "wasserstein": wasserstein(reference, current),
        "wasserstein_norm": wasserstein_norm(reference, current),
        "jensenshannon": js_distance(reference, current, True),
        "mean_reference": fsum_mean(reference),
        "mean_current": fsum_mean(current),
    }


def categorical_stats(reference: List[str], current: List[str]) -> Dict[str, Any]:
    statistic, p_value, df = chi_square(reference, current)
    ref_shares, cur_shares, keys = binned_shares(reference, current, False)
    return {
        "psi": psi(reference, current, False),
        "chisquare_statistic": statistic,
        "chisquare_p_value": p_value,
        "chisquare_df": df,
        "jensenshannon": jensen_shannon(ref_shares, cur_shares),
        "shares_reference": {str(k): round(v, 6) for k, v in zip(keys, ref_shares)},
        "shares_current": {str(k): round(v, 6) for k, v in zip(keys, cur_shares)},
    }


def tokens(text: str) -> List[str]:
    return TOKEN.findall(text.lower())


def text_stats(reference: List[str], current: List[str], oov_threshold: float) -> Dict[str, Any]:
    ref_tokens = [tokens(t) for t in reference]
    cur_tokens = [tokens(t) for t in current]
    length_stat, length_p, _ = ks_2samp([float(len(t)) for t in ref_tokens], [float(len(t)) for t in cur_tokens])
    ref_counts = Counter(tok for row in ref_tokens for tok in row)
    cur_counts = Counter(tok for row in cur_tokens for tok in row)
    vocabulary = sorted(set(ref_counts) | set(cur_counts))
    cur_total = sum(cur_counts.values()) or 1
    unseen = Counter({tok: n for tok, n in cur_counts.items() if tok not in ref_counts})
    oov_rate = sum(unseen.values()) / cur_total
    return {
        "length_ks_statistic": length_stat,
        "length_ks_p_value": length_p,
        "mean_tokens_reference": fsum_mean([len(t) for t in ref_tokens]),
        "mean_tokens_current": fsum_mean([len(t) for t in cur_tokens]),
        "token_jensenshannon": jensen_shannon([ref_counts[t] for t in vocabulary], [cur_counts[t] for t in vocabulary]),
        "oov_rate": oov_rate,
        "oov_threshold": oov_threshold,
        "top_new_tokens": [tok for tok, _ in sorted(unseen.items(), key=lambda kv: (-kv[1], kv[0]))[:5]],
    }


def compare_column(name: str, kind: str, role: str, ref: ColumnData, cur: ColumnData, options: Options) -> ColumnResult:
    if not ref.values or not cur.values:
        raise InputError(f"column {name!r} has no usable values in the {'reference' if not ref.values else 'current'} window")
    if kind == "text":
        stats = text_stats(ref.values, cur.values, options.oov_threshold)
        threshold = options.thresholds["jensenshannon"]
        drifted = (
            stats["token_jensenshannon"] >= threshold
            or stats["oov_rate"] >= options.oov_threshold
            or stats["length_ks_p_value"] < options.thresholds["ks"]
        )
        return ColumnResult(name, kind, role, "text", stats["token_jensenshannon"], threshold, drifted, stats)
    override = options.cat_method if kind == "categorical" else options.num_method
    method = choose_method(kind, len(ref.values), override)
    stats = categorical_stats(ref.values, cur.values) if kind == "categorical" else numerical_stats(ref.values, cur.values)
    key = {"ks": "ks_p_value", "chisquare": "chisquare_p_value", "wasserstein": "wasserstein_norm"}.get(method, method)
    if key not in stats:
        raise InputError(f"method {method!r} does not apply to {kind} column {name!r}")
    threshold = options.thresholds[method]
    score = stats[key]
    return ColumnResult(name, kind, role, method, score, threshold, is_drift(method, score, threshold), stats)


def read_csv(path: str) -> Tuple[List[str], List[Dict[str, str]]]:
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            header = list(reader.fieldnames or [])
    except FileNotFoundError:
        raise InputError(f"{path}: no such file")
    except (UnicodeDecodeError, csv.Error) as exc:
        raise InputError(f"{path}: not a readable CSV file ({exc})")
    if not header:
        raise InputError(f"{path}: empty file, expected a header row")
    return header, rows


def read_schema(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise InputError(f"{path}: no such file")
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: invalid JSON ({exc})")
    if not isinstance(data, dict):
        raise InputError(f"{path}: the schema must be a JSON object")
    return data


def infer_kind(values: List[str]) -> str:
    present = [v for v in values if not is_missing(v)]
    if not present:
        return "categorical"
    numeric = sum(parse_float(v) is not None for v in present)
    if numeric >= NUMERIC_SHARE * len(present):
        return "numerical"
    if fsum_mean([len(tokens(v)) for v in present]) >= TEXT_MIN_TOKENS:
        return "text"
    return "categorical"


def build_schema(header: List[str], rows: List[Dict[str, str]], given: Dict[str, Any]) -> Dict[str, Tuple[str, str]]:
    roles = {given.get("id", "id"): "id", given.get("target", "label"): "target", given.get("prediction", "prediction"): "prediction"}
    kinds: Dict[str, str] = {}
    for kind in ("numerical", "categorical", "text"):
        for name in given.get(kind, []):
            kinds[name] = kind
    unknown = [name for name in kinds if name not in header]
    if unknown:
        raise InputError(f"schema names columns that are not in the reference file: {', '.join(unknown)}")
    schema: Dict[str, Tuple[str, str]] = {}
    for name in header:
        role = roles.get(name, "feature")
        if role in ("id", "target"):
            schema[name] = ("", role)
            continue
        schema[name] = (kinds.get(name) or infer_kind([r.get(name, "") for r in rows]), role)
    return schema


def column_data(rows: List[Dict[str, str]], name: str, kind: str) -> ColumnData:
    data = ColumnData(values=[], rows=len(rows))
    for row in rows:
        raw = row.get(name)
        if is_missing(raw):
            data.missing += 1
            continue
        if kind == "numerical":
            value = parse_float(raw)
            if value is None:
                data.type_errors += 1
                continue
            data.values.append(value)
        else:
            data.values.append(raw.strip())
    return data


def quality(name: str, kind: str, ref: ColumnData, cur: ColumnData, options: Options) -> Dict[str, Any]:
    ref_missing = ref.missing / ref.rows if ref.rows else 0.0
    cur_missing = cur.missing / cur.rows if cur.rows else 0.0
    report: Dict[str, Any] = {"column": name, "kind": kind, "missing_reference": ref_missing, "missing_current": cur_missing,
                              "type_errors": cur.type_errors, "out_of_range": 0.0, "new_categories": [], "issues": []}
    if cur_missing - ref_missing > options.max_missing_increase:
        report["issues"].append(f"missing rate rose from {ref_missing:.1%} to {cur_missing:.1%}")
    if cur.type_errors:
        report["issues"].append(f"{cur.type_errors} value(s) are not {kind}")
    if kind == "numerical" and ref.values and cur.values:
        lo, hi = min(ref.values), max(ref.values)
        outside = sum(1 for v in cur.values if v < lo or v > hi) / len(cur.values)
        report["out_of_range"] = outside
        if outside > options.max_out_of_range:
            report["issues"].append(f"{outside:.1%} of values outside the reference range [{lo:g}, {hi:g}]")
    if kind == "categorical":
        seen = set(ref.values)
        new = Counter(v for v in cur.values if v not in seen)
        report["new_categories"] = sorted(new)
        if new:
            share = sum(new.values()) / len(cur.values)
            report["issues"].append(f"{len(new)} new categor{'y' if len(new) == 1 else 'ies'}: {', '.join(sorted(new))} ({share:.1%} of rows)")
    return report


def read_labels(path: Optional[str], id_column: str, target: str) -> Dict[str, str]:
    if not path:
        return {}
    header, rows = read_csv(path)
    if id_column not in header or target not in header:
        raise InputError(f"{path}: needs columns {id_column!r} and {target!r}, found {', '.join(header)}")
    return {r[id_column]: r[target] for r in rows if not is_missing(r[target])}


def score_rows(pairs: List[Tuple[float, float]], task: str) -> float:
    if task == "classification":
        return sum(1 for y, p in pairs if (p >= 0.5) == (y >= 0.5)) / len(pairs)
    return fsum_mean([abs(y - p) for y, p in pairs])


def performance(ref_rows: List[Dict[str, str]], cur_rows: List[Dict[str, str]], labels: Dict[str, str],
                id_column: str, target: str, prediction: str, options: Options) -> Optional[Dict[str, Any]]:
    def pairs(rows: List[Dict[str, str]], late: Dict[str, str]) -> List[Tuple[float, float]]:
        out = []
        for row in rows:
            raw = row.get(target)
            raw = late.get(row.get(id_column, ""), raw) if is_missing(raw) else raw
            y, p = parse_float(raw or ""), parse_float(row.get(prediction) or "")
            if y is not None and p is not None:
                out.append((y, p))
        return out

    ref_pairs, cur_pairs = pairs(ref_rows, {}), pairs(cur_rows, labels)
    if not ref_pairs:
        return None
    task = "classification" if all(y in (0.0, 1.0) for y, _ in ref_pairs) else "regression"
    metric = "accuracy" if task == "classification" else "mae"
    coverage = len(cur_pairs) / len(cur_rows) if cur_rows else 0.0
    result: Dict[str, Any] = {"task": task, "metric": metric, "reference": score_rows(ref_pairs, task), "current": None,
                              "labelled_current": len(cur_pairs), "coverage": coverage, "decay": None, "status": "insufficient_labels"}
    if cur_pairs:
        result["current"] = score_rows(cur_pairs, task)
        result["decay"] = result["reference"] - result["current"] if task == "classification" else result["current"] - result["reference"]
    if coverage >= options.min_label_coverage and result["decay"] is not None:
        result["status"] = "decayed" if result["decay"] > options.decay_threshold else "ok"
    return result


def analyze(reference_path: str, current_path: str, options: Options, schema_path: Optional[str] = None,
            labels_path: Optional[str] = None) -> Dict[str, Any]:
    ref_header, ref_rows = read_csv(reference_path)
    cur_header, cur_rows = read_csv(current_path)
    if not ref_rows or not cur_rows:
        raise InputError(f"{'reference' if not ref_rows else 'current'} window has no rows")
    given = read_schema(schema_path)
    schema = build_schema(ref_header, ref_rows, given)
    missing_columns = [c for c, (_, role) in schema.items() if role in ("feature", "prediction") and c not in cur_header]
    if missing_columns:
        raise InputError(f"current window lacks column(s) {', '.join(missing_columns)}: schema change, not drift")
    columns, checks = [], []
    for name, (kind, role) in schema.items():
        if role not in ("feature", "prediction"):
            continue
        ref, cur = column_data(ref_rows, name, kind), column_data(cur_rows, name, kind)
        checks.append(quality(name, kind, ref, cur, options))
        columns.append(compare_column(name, kind, role, ref, cur, options))
    features = [c for c in columns if c.role == "feature"]
    drifted = [c.column for c in features if c.drifted]
    share = len(drifted) / len(features) if features else 0.0
    id_column = given.get("id", "id")
    target = given.get("target", "label")
    prediction = given.get("prediction", "prediction")
    perf = None
    if target in ref_header and prediction in ref_header:
        perf = performance(ref_rows, cur_rows, read_labels(labels_path, id_column, target), id_column, target, prediction, options)
    return {
        "model": options.model,
        "generated_at": time.time(),
        "rows": {"reference": len(ref_rows), "current": len(cur_rows)},
        "columns": [asdict(c) for c in columns],
        "quality": checks,
        "performance": perf,
        "dataset": {"features": len(features), "drifted": len(drifted), "drifted_columns": drifted,
                    "share": share, "drift_share": options.drift_share, "drift": bool(features) and share >= options.drift_share},
    }


def needs_attention(report: Dict[str, Any]) -> bool:
    perf = report.get("performance") or {}
    return any(c["drifted"] for c in report["columns"]) or perf.get("status") == "decayed" or any(q["issues"] for q in report["quality"])


def render_text(report: Dict[str, Any]) -> str:
    rows = report["rows"]
    lines = [f"minidrift: model {report['model']}, reference {rows['reference']:,} rows, current {rows['current']:,} rows", ""]
    lines.append(f"{'column':<14} {'kind':<12} {'role':<11} {'method':<14} {'score':>10} {'threshold':>9}  drift")
    for c in report["columns"]:
        flag = "DRIFT" if c["drifted"] else "-"
        lines.append(f"{c['column']:<14} {c['kind']:<12} {c['role']:<11} {c['method']:<14} {c['score']:>10.4g} {c['threshold']:>9g}  {flag}")
    ds = report["dataset"]
    verdict = "dataset drift" if ds["drift"] else "below the dataset drift share"
    lines += ["", f"dataset: {ds['drifted']} of {ds['features']} features drifted ({ds['share']:.0%}), {verdict} ({ds['drift_share']:.0%})"]
    perf = report.get("performance")
    if perf:
        current = "n/a" if perf["current"] is None else f"{perf['current']:.3f}"
        lines.append(f"performance: {perf['metric']} {perf['reference']:.3f} -> {current} on {perf['labelled_current']:,} labelled rows "
                     f"({perf['coverage']:.0%} coverage): {perf['status']}")
    issues = [(q["column"], issue) for q in report["quality"] for issue in q["issues"]]
    lines.append("quality:" if issues else "quality: no issues")
    lines += [f"  {column}: {issue}" for column, issue in issues]
    for c in report["columns"]:
        if c["kind"] == "text":
            s = c["stats"]
            lines.append(f"text {c['column']}: tokens {s['mean_tokens_reference']:.1f} -> {s['mean_tokens_current']:.1f}, "
                         f"token JS {s['token_jensenshannon']:.3f}, OOV {s['oov_rate']:.1%}, new: {', '.join(s['top_new_tokens']) or '-'}")
    return "\n".join(lines) + "\n"


def label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def sample_value(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return repr(float(value))


METRIC_HELP = {
    "minidrift_feature_drift_score": "Drift score of each column under its decision method: a distance, or a p-value for ks and chisquare.",
    "minidrift_feature_drift_threshold": "Threshold the drift score is compared with.",
    "minidrift_feature_drift_detected": "1 if the column drifted between the reference and current windows, else 0.",
    "minidrift_feature_psi": "Population stability index of each column.",
    "minidrift_feature_missing_ratio": "Share of missing values per column and window.",
    "minidrift_feature_type_errors": "Values in the current window that do not parse as the column's type.",
    "minidrift_feature_out_of_range_ratio": "Share of current numerical values outside the reference minimum and maximum.",
    "minidrift_feature_new_categories": "Categories seen in the current window but never in the reference window.",
    "minidrift_text_oov_ratio": "Share of current tokens that never appear in the reference window.",
    "minidrift_dataset_drift_share": "Share of feature columns that drifted.",
    "minidrift_dataset_drift_detected": "1 if the drifted share reaches the dataset drift share, else 0.",
    "minidrift_performance_metric": "Model quality per window on labelled rows (accuracy or mae).",
    "minidrift_label_coverage_ratio": "Share of current rows whose label has arrived.",
    "minidrift_window_rows": "Rows per window.",
    "minidrift_last_run_timestamp_seconds": "Unix time of the last drift computation.",
}


def render_metrics(report: Dict[str, Any]) -> str:
    samples: Dict[str, List[Tuple[Dict[str, str], float]]] = {name: [] for name in METRIC_HELP}
    model = report["model"]

    def add(name: str, value: Optional[float], **labels: str) -> None:
        if value is not None:
            samples[name].append(({"model": model, **labels}, float(value)))

    for c in report["columns"]:
        base = {"feature": c["column"], "kind": c["kind"], "role": c["role"]}
        add("minidrift_feature_drift_score", c["score"], method=c["method"], **base)
        add("minidrift_feature_drift_threshold", c["threshold"], method=c["method"], **base)
        add("minidrift_feature_drift_detected", 1.0 if c["drifted"] else 0.0, method=c["method"], **base)
        add("minidrift_feature_psi", c["stats"].get("psi"), **base)
        add("minidrift_text_oov_ratio", c["stats"].get("oov_rate"), feature=c["column"])
    for q in report["quality"]:
        add("minidrift_feature_missing_ratio", q["missing_reference"], feature=q["column"], window="reference")
        add("minidrift_feature_missing_ratio", q["missing_current"], feature=q["column"], window="current")
        add("minidrift_feature_type_errors", q["type_errors"], feature=q["column"])
        if q["kind"] == "numerical":
            add("minidrift_feature_out_of_range_ratio", q["out_of_range"], feature=q["column"])
        if q["kind"] == "categorical":
            add("minidrift_feature_new_categories", len(q["new_categories"]), feature=q["column"])
    ds = report["dataset"]
    add("minidrift_dataset_drift_share", ds["share"])
    add("minidrift_dataset_drift_detected", 1.0 if ds["drift"] else 0.0)
    perf = report.get("performance")
    if perf:
        add("minidrift_performance_metric", perf["reference"], metric=perf["metric"], window="reference")
        add("minidrift_performance_metric", perf["current"], metric=perf["metric"], window="current")
        add("minidrift_label_coverage_ratio", perf["coverage"])
    add("minidrift_window_rows", report["rows"]["reference"], window="reference")
    add("minidrift_window_rows", report["rows"]["current"], window="current")
    add("minidrift_last_run_timestamp_seconds", report["generated_at"])
    lines = []
    for name, help_text in METRIC_HELP.items():
        if not samples[name]:
            continue
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
        for labels, value in samples[name]:
            rendered = ",".join(f'{k}="{label_value(v)}"' for k, v in labels.items())
            lines.append(f"{name}{{{rendered}}} {sample_value(value)}")
    return "\n".join(lines) + "\n"


def render_rules(hold: str = "30m", drift_share: float = 0.5, missing_increase: float = 0.05, stale_seconds: int = 7200) -> str:
    return f"""groups:
  - name: minidrift
    rules:
      - alert: FeatureDrift
        expr: minidrift_feature_drift_detected{{role="feature"}} == 1
        for: {hold}
        labels:
          severity: ticket
        annotations:
          summary: "{{{{ $labels.feature }}}} of {{{{ $labels.model }}}} drifted ({{{{ $labels.method }}}})"
      - alert: PredictionDrift
        expr: minidrift_feature_drift_detected{{role="prediction"}} == 1
        for: {hold}
        labels:
          severity: ticket
        annotations:
          summary: "predictions of {{{{ $labels.model }}}} drifted ({{{{ $labels.method }}}})"
      - alert: DatasetDrift
        expr: minidrift_dataset_drift_share >= {drift_share}
        for: {hold}
        labels:
          severity: page
        annotations:
          summary: "{{{{ $value | humanizePercentage }}}} of the features of {{{{ $labels.model }}}} drifted"
      - alert: FeatureMissingValues
        expr: minidrift_feature_missing_ratio{{window="current"}} - ignoring(window) minidrift_feature_missing_ratio{{window="reference"}} > {missing_increase}
        for: {hold}
        labels:
          severity: ticket
        annotations:
          summary: "{{{{ $labels.feature }}}} of {{{{ $labels.model }}}} lost values: a pipeline bug until proven otherwise"
      - alert: DriftCheckStale
        expr: time() - minidrift_last_run_timestamp_seconds > {stale_seconds}
        for: 10m
        labels:
          severity: ticket
        annotations:
          summary: "the drift check for {{{{ $labels.model }}}} has not run for {{{{ $value | humanizeDuration }}}}"
"""


class MetricsState:
    def __init__(self, args: argparse.Namespace, options: Options) -> None:
        self.args, self.options = args, options
        self.lock = threading.Lock()
        self.stamp: Tuple[float, ...] = ()
        self.report: Dict[str, Any] = {}
        self.runs = 0

    def paths(self) -> List[str]:
        return [p for p in (self.args.reference, self.args.current, self.args.labels, self.args.schema) if p]

    def current(self) -> Dict[str, Any]:
        with self.lock:
            stamp = tuple(os.stat(p).st_mtime_ns for p in self.paths())
            if stamp != self.stamp:
                self.report = analyze(self.args.reference, self.args.current, self.options, self.args.schema, self.args.labels)
                self.stamp = stamp
                self.runs += 1
            return self.report


def make_handler(state: MetricsState) -> type:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path not in ("/metrics", "/report", "/healthz"):
                return self.send(404, "text/plain; charset=utf-8", "not found: try /metrics or /report\n")
            if path == "/healthz":
                return self.send(200, "text/plain; charset=utf-8", "ok\n")
            try:
                report = state.current()
            except (InputError, OSError) as exc:
                return self.send(500, "text/plain; charset=utf-8", f"drift computation failed: {exc}\n")
            if path == "/report":
                return self.send(200, "application/json", json.dumps(report, indent=2) + "\n")
            return self.send(200, "text/plain; version=0.0.4; charset=utf-8", render_metrics(report))

        def send(self, status: int, content_type: str, body: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


def make_server(args: argparse.Namespace, options: Options) -> http.server.ThreadingHTTPServer:
    state = MetricsState(args, options)
    state.current()
    server = http.server.ThreadingHTTPServer((args.host, args.port), make_handler(state))
    server.daemon_threads = True
    server.state = state
    return server


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


BASE_WORDS = ["my", "order", "has", "not", "arrived", "where", "is", "the", "package", "please", "help", "tracking",
              "number", "delivery", "late", "address", "change", "thanks", "when", "will", "it", "ship", "today"]
NEW_WORDS = ["refund", "charged", "twice", "card", "outage", "app", "crash", "login"]


def make_row(rng: random.Random, row_id: int, shifted: bool) -> Dict[str, Any]:
    age: Any = min(90, max(18, round(rng.gauss(41, 11))))
    log_income = rng.gauss(10.8 + (0.25 if shifted else 0.0), 0.45)
    tenure: Any = rng.randint(0, 2000)
    plan = rng.choices(["basic", "plus", "pro"], weights=[40, 35, 25] if shifted else [60, 30, 10])[0]
    region = rng.choices(["eu", "us", "latam"], weights=[45, 40, 15])[0]
    if shifted and rng.random() < 0.012:
        region = "apac"
    length = rng.randint(6, 20) if shifted else rng.randint(4, 12)
    words = [rng.choice(BASE_WORDS) for _ in range(length)]
    if shifted and rng.random() < 0.5:
        words[rng.randrange(length)] = rng.choice(NEW_WORDS)
        words[rng.randrange(length)] = rng.choice(NEW_WORDS)
    z = (log_income - 10.8) / 0.45
    model_logit = 0.9 * z + 0.8 * (plan == "pro") - 0.3 * (plan == "basic") - 0.2
    true_logit = model_logit - (1.2 * (plan == "pro") + 0.4 * z if shifted else 0.0)
    label = 1 if rng.random() < sigmoid(2.5 * true_logit) else 0
    if shifted and rng.random() < 0.06:
        age = ""
    if shifted and rng.random() < 0.004:
        tenure = "unknown"
    return {"id": f"r{row_id:06d}", "age": age, "income": round(math.exp(log_income), 2), "tenure_days": tenure, "plan": plan,
            "region": region, "message": " ".join(words), "prediction": round(sigmoid(model_logit), 4), "label": label}


def generate(out: Path, rows: int, seed: int, label_share: float = 0.4) -> Dict[str, str]:
    rng = random.Random(seed)
    reference = [make_row(rng, i, False) for i in range(rows)]
    current = [make_row(rng, rows + i, True) for i in range(rows)]
    arrived = [{"id": r["id"], "label": r["label"]} for r in current if rng.random() < label_share]
    for row in current:
        row["label"] = ""
    out.mkdir(parents=True, exist_ok=True)
    files = {"reference": out / "reference.csv", "current": out / "current.csv", "labels": out / "labels.csv", "schema": out / "schema.json"}
    for key, data in (("reference", reference), ("current", current), ("labels", arrived)):
        with open(files[key], "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(data[0]) if data else ["id", "label"])
            writer.writeheader()
            writer.writerows(data)
    schema = {"id": "id", "target": "label", "prediction": "prediction", "numerical": ["age", "income", "tenure_days"],
              "categorical": ["plan", "region"], "text": ["message"]}
    files["schema"].write_text(json.dumps(schema, indent=2) + "\n")
    return {k: str(v) for k, v in files.items()}


def parse_threshold(text: str) -> Tuple[str, float]:
    method, _, value = text.partition("=")
    if method not in DEFAULT_THRESHOLDS or parse_float(value) is None:
        raise argparse.ArgumentTypeError(f"expected METHOD=VALUE with METHOD in {', '.join(DEFAULT_THRESHOLDS)}, got {text!r}")
    return method, float(value)


def options_from(args: argparse.Namespace) -> Options:
    thresholds = dict(DEFAULT_THRESHOLDS)
    thresholds.update(dict(args.threshold or []))
    return Options(model=args.model, num_method=args.num_method, cat_method=args.cat_method, thresholds=thresholds,
                   drift_share=args.drift_share)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minidrift", description="Data drift and data-quality checks between a reference and a current window.")
    sub = parser.add_subparsers(dest="command", required=True)
    windows = argparse.ArgumentParser(add_help=False)
    windows.add_argument("reference", help="CSV of the reference window (training or a known-good period)")
    windows.add_argument("current", help="CSV of the current window (recent production inputs)")
    windows.add_argument("--schema", help="JSON with id, target, prediction and numerical/categorical/text column lists")
    windows.add_argument("--labels", help="CSV of late-arriving labels (id and target columns) for the current window")
    windows.add_argument("--model", default="model", help="model name used in the report and metric labels")
    windows.add_argument("--num-method", choices=NUMERICAL_METHODS, help="drift method for numerical columns (default: ks up to 1000 reference rows, else wasserstein)")
    windows.add_argument("--cat-method", choices=CATEGORICAL_METHODS, help="drift method for categorical columns (default: chisquare up to 1000 reference rows, else jensenshannon)")
    windows.add_argument("--threshold", action="append", type=parse_threshold, metavar="METHOD=VALUE", help="override a method's threshold")
    windows.add_argument("--drift-share", type=float, default=0.5, help="share of drifted features that means dataset drift")
    report = sub.add_parser("report", parents=[windows], help="compare two windows and print the report")
    report.add_argument("--json", action="store_true", help="machine-readable output")
    report.add_argument("--fail-on-drift", action="store_true", help="exit 1 when a column drifted, quality failed or performance decayed")
    sub.add_parser("metrics", parents=[windows], help="print the report as Prometheus text exposition")
    serve = sub.add_parser("serve", parents=[windows], help="serve /metrics and /report; recomputes when an input file changes")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=9108, help="0 picks a free port")
    serve.add_argument("--port-file", help="write the bound port here once listening")
    rules = sub.add_parser("rules", help="print Prometheus alerting rules for the drift gauges")
    rules.add_argument("--for", dest="hold", default="30m", help="how long a condition must hold before the alert fires")
    rules.add_argument("--drift-share", type=float, default=0.5)
    gen = sub.add_parser("generate", help="write seeded reference/current/labels CSVs with planted shifts")
    gen.add_argument("--out", required=True)
    gen.add_argument("--rows", type=int, default=2000)
    gen.add_argument("--seed", type=int, default=7)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    try:
        if args.command == "generate":
            if args.rows < 1:
                raise InputError("--rows must be at least 1")
            files = generate(Path(args.out), args.rows, args.seed)
            print(json.dumps(files, indent=2))
            return 0
        if args.command == "rules":
            sys.stdout.write(render_rules(args.hold, args.drift_share))
            return 0
        options = options_from(args)
        if args.command == "serve":
            server = make_server(args, options)
            port = server.server_address[1]
            if args.port_file:
                Path(args.port_file).write_text(f"{port}\n")
            print(f"minidrift serving http://{args.host}:{port}/metrics", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        report = analyze(args.reference, args.current, options, args.schema, args.labels)
        if args.command == "metrics":
            sys.stdout.write(render_metrics(report))
            return 0
        sys.stdout.write(json.dumps(report, indent=2) + "\n" if args.json else render_text(report))
        return 1 if args.fail_on_drift and needs_attention(report) else 0
    except InputError as exc:
        print(f"minidrift: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
