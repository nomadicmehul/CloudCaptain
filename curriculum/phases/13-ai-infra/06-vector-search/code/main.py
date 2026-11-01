"""vecindex: exact, IVF and HNSW nearest-neighbour search over float vectors in pure Python, with L2 and cosine, metadata filters (post, pre, iterative), upserts and tombstones, recall@k against brute force, memory and work estimates, snapshots, and the SQL and grading helpers that run the same benchmark against pgvector."""
from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

FORMAT = "vecindex/1"
METRICS = ("l2", "cosine")
KINDS = ("flat", "ivf", "hnsw")
FILTER_MODES = ("post", "pre", "iterative")
KNOBS = {"flat": "exact", "ivf": "probes", "hnsw": "ef_search"}
PG_OPERATORS = {"l2": "<->", "cosine": "<=>"}
PG_OPCLASSES = {"l2": "vector_l2_ops", "cosine": "vector_cosine_ops"}
PG_INDEX = "items_embedding_ann"
FLOAT_BYTES = 4
LINK_BYTES = 4
DEFAULT_EF_SEARCH = 40
DEFAULT_MAX_VISIT = 20000
DEFAULT_BANDWIDTH_GBPS = 10.0
DEFAULT_NLIST = 40
DEFAULT_VALUES = {"flat": [1], "ivf": [1, 2, 4, 8, 16], "hnsw": [10, 20, 40, 80, 200]}

Scored = Tuple[float, int]


class InputError(ValueError):
    pass


def parse_vector_text(text: str) -> List[str]:
    raw = text.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return raw.replace(",", " ").split()


def as_vector(values: Any, dim: Optional[int] = None) -> List[float]:
    if isinstance(values, str):
        values = parse_vector_text(values)
    try:
        vector = [float(v) for v in values]
    except (TypeError, ValueError):
        raise InputError(f"not a vector of numbers: {str(values)[:60]} (example: [0.1,0.2,0.3])")
    if not vector:
        raise InputError("a vector needs at least one dimension")
    if not all(math.isfinite(v) for v in vector):
        raise InputError("vector elements must be finite numbers; pgvector rejects NaN and Infinity as well")
    if dim is not None and len(vector) != dim:
        raise InputError(f"expected {dim} dimensions, got {len(vector)}")
    return vector


def norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(v * v for v in vector))


def unit(vector: Sequence[float]) -> List[float]:
    length = norm(vector)
    if length == 0.0:
        raise InputError("a zero vector has no direction, so its cosine distance is undefined; pgvector does not index zero vectors for cosine either")
    return [v / length for v in vector]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def l2_distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.dist(a, b)


def cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    return 1.0 - dot(a, b) / (norm(a) * norm(b))


@dataclass
class Space:
    metric: str
    dim: int

    def __post_init__(self) -> None:
        if self.metric not in METRICS:
            raise InputError(f"metric must be one of {', '.join(METRICS)}, got {self.metric!r}")
        if self.dim < 1:
            raise InputError("dim must be at least 1")

    def prepare(self, vector: Any) -> List[float]:
        checked = as_vector(vector, self.dim)
        return unit(checked) if self.metric == "cosine" else checked

    def report(self, rank_distance: float) -> float:
        return rank_distance * rank_distance / 2.0 if self.metric == "cosine" else rank_distance


@dataclass
class Node:
    key: str
    vector: List[float]
    meta: Dict[str, Any]
    deleted: bool = False


@dataclass
class Hit:
    key: str
    distance: float
    meta: Dict[str, Any]


@dataclass
class SearchResult:
    hits: List[Hit]
    evals: int
    mode: str
    note: str = ""

    @property
    def keys(self) -> List[str]:
        return [hit.key for hit in self.hits]


def parse_filter(text: Optional[str]) -> Dict[str, Any]:
    if not text:
        return {}
    result: Dict[str, Any] = {}
    for part in text.split(","):
        key, sep, value = part.partition("=")
        if not sep or not key.strip():
            raise InputError(f"filter {part!r} is not key=value (example: tag=3,lang=en)")
        try:
            result[key.strip()] = json.loads(value)
        except ValueError:
            result[key.strip()] = value
    return result


def matches(meta: Dict[str, Any], flt: Dict[str, Any]) -> bool:
    return all(meta.get(key) == value for key, value in flt.items())


class Index:
    kind = "flat"

    def __init__(self, space: Space):
        self.space = space
        self.nodes: List[Node] = []
        self.by_key: Dict[str, int] = {}

    @property
    def params(self) -> Dict[str, Any]:
        return {}

    @property
    def live(self) -> int:
        return len(self.by_key)

    @property
    def tombstones(self) -> int:
        return len(self.nodes) - len(self.by_key)

    def upsert(self, key: Any, vector: Any, meta: Optional[Dict[str, Any]] = None) -> int:
        prepared = self.space.prepare(vector)
        name = str(key)
        old = self.by_key.get(name)
        if old is not None:
            self.nodes[old].deleted = True
        node = len(self.nodes)
        self.nodes.append(Node(name, prepared, dict(meta or {})))
        self.by_key[name] = node
        self._add(node)
        return node

    def delete(self, key: Any) -> bool:
        node = self.by_key.pop(str(key), None)
        if node is None:
            return False
        self.nodes[node].deleted = True
        return True

    def _add(self, node: int) -> None:
        return None

    def accepts(self, flt: Optional[Dict[str, Any]]) -> Callable[[int], bool]:
        nodes = self.nodes
        if not flt:
            return lambda n: not nodes[n].deleted
        return lambda n: not nodes[n].deleted and matches(nodes[n].meta, flt)

    def hits(self, scored: Iterable[Scored]) -> List[Hit]:
        return [Hit(self.nodes[n].key, self.space.report(d), self.nodes[n].meta) for d, n in scored]

    def exact(self, query: List[float], k: int, flt: Optional[Dict[str, Any]] = None) -> SearchResult:
        accept = self.accepts(flt)
        pool = [n for n in range(len(self.nodes)) if accept(n)]
        best = heapq.nsmallest(k, ((math.dist(query, self.nodes[n].vector), n) for n in pool))
        return SearchResult(self.hits(best), len(pool), "pre" if flt else "exact")

    def search(self, vector: Any, k: int = 10, flt: Optional[Dict[str, Any]] = None, mode: str = "post", **knobs: Any) -> SearchResult:
        if k < 1:
            raise InputError("k must be at least 1")
        if mode not in FILTER_MODES:
            raise InputError(f"filter mode must be one of {', '.join(FILTER_MODES)}, got {mode!r}")
        query = self.space.prepare(vector)
        if flt and mode == "pre":
            return self.exact(query, k, flt)
        return self._search(query, k, flt or {}, mode, **knobs)

    def _search(self, query: List[float], k: int, flt: Dict[str, Any], mode: str, **knobs: Any) -> SearchResult:
        live = [n for n in range(len(self.nodes)) if not self.nodes[n].deleted]
        scored = ((math.dist(query, self.nodes[n].vector), n) for n in live)
        best = heapq.nsmallest(k, ((d, n) for d, n in scored if matches(self.nodes[n].meta, flt)))
        return SearchResult(self.hits(best), len(live), mode if flt else "exact")

    def stats(self) -> Dict[str, Any]:
        return {"kind": self.kind, "metric": self.space.metric, "dim": self.space.dim, "live": self.live, "tombstones": self.tombstones, "params": self.params}


def nearest_index(vector: Sequence[float], centroids: Sequence[Sequence[float]]) -> int:
    return min(range(len(centroids)), key=lambda i: math.dist(vector, centroids[i]))


def kmeans_plus_plus(vectors: Sequence[List[float]], k: int, rng: random.Random) -> List[List[float]]:
    centroids = [list(vectors[rng.randrange(len(vectors))])]
    closest = [math.dist(v, centroids[0]) ** 2 for v in vectors]
    while len(centroids) < k:
        total = sum(closest)
        pick = rng.randrange(len(vectors))
        if total > 0:
            target, running = rng.random() * total, 0.0
            for i, weight in enumerate(closest):
                running += weight
                if running >= target:
                    pick = i
                    break
        centroids.append(list(vectors[pick]))
        closest = [min(c, math.dist(v, centroids[-1]) ** 2) for c, v in zip(closest, vectors)]
    return centroids


def kmeans(vectors: Sequence[List[float]], k: int, rng: random.Random, iterations: int = 25, spherical: bool = False) -> Tuple[List[List[float]], List[int]]:
    centroids = kmeans_plus_plus(vectors, k, rng)
    assignment = [-1] * len(vectors)
    dim = len(vectors[0])
    for _ in range(iterations):
        changed = False
        for i, vector in enumerate(vectors):
            best = nearest_index(vector, centroids)
            if best != assignment[i]:
                assignment[i], changed = best, True
        if not changed:
            break
        sums = [[0.0] * dim for _ in centroids]
        counts = [0] * len(centroids)
        for vector, cluster in zip(vectors, assignment):
            counts[cluster] += 1
            total = sums[cluster]
            for j, x in enumerate(vector):
                total[j] += x
        for c, count in enumerate(counts):
            if count:
                mean = [x / count for x in sums[c]]
                centroids[c] = unit(mean) if spherical and norm(mean) > 0 else mean
    return centroids, assignment


class IVF(Index):
    kind = "ivf"

    def __init__(self, space: Space, nlist: int = DEFAULT_NLIST, seed: int = 0, iterations: int = 25):
        super().__init__(space)
        if nlist < 1:
            raise InputError("nlist must be at least 1")
        self.nlist, self.seed, self.iterations = nlist, seed, iterations
        self.centroids: List[List[float]] = []
        self.lists: List[List[int]] = []
        self.trained_on = 0
        self.warnings: List[str] = []

    @property
    def params(self) -> Dict[str, Any]:
        return {"nlist": self.nlist, "seed": self.seed, "iterations": self.iterations}

    def train(self) -> None:
        live = sorted(self.by_key.values())
        if not live:
            raise InputError("IVF learns its centroids from the vectors already loaded; load data first, then build the index")
        lists = min(self.nlist, len(live))
        self.warnings = []
        if lists < self.nlist:
            self.warnings.append(f"only {len(live)} vectors for nlist={self.nlist}: trained {lists} lists; expect low recall until you rebuild with more data")
        vectors = [self.nodes[n].vector for n in live]
        self.centroids, assignment = kmeans(vectors, lists, random.Random(self.seed), self.iterations, self.space.metric == "cosine")
        self.lists = [[] for _ in self.centroids]
        for node, cluster in zip(live, assignment):
            self.lists[cluster].append(node)
        self.trained_on = len(live)

    def _add(self, node: int) -> None:
        if self.centroids:
            self.lists[nearest_index(self.nodes[node].vector, self.centroids)].append(node)

    def _search(self, query: List[float], k: int, flt: Dict[str, Any], mode: str, nprobe: int = 1, max_probes: Optional[int] = None, **knobs: Any) -> SearchResult:
        if not self.centroids:
            raise InputError("the IVF index is not trained; build it after loading vectors")
        order = sorted((math.dist(query, c), i) for i, c in enumerate(self.centroids))
        nprobe = max(1, min(nprobe, len(order)))
        limit = len(order) if max_probes is None else max(nprobe, min(max_probes, len(order)))
        accept = self.accepts(flt)
        evals, probed, scored = len(order), 0, []
        for _, cluster in order:
            if probed >= nprobe and not (mode == "iterative" and len(scored) < k and probed < limit):
                break
            probed += 1
            for n in self.lists[cluster]:
                if self.nodes[n].deleted:
                    continue
                evals += 1
                distance = math.dist(query, self.nodes[n].vector)
                if accept(n):
                    scored.append((distance, n))
        best = heapq.nsmallest(k, scored)
        return SearchResult(self.hits(best), evals, mode if flt else "ann", f"probed {probed} of {len(order)} lists")

    def stats(self) -> Dict[str, Any]:
        sizes = [sum(1 for n in members if not self.nodes[n].deleted) for members in self.lists]
        mean = sum(sizes) / len(sizes) if sizes else 0.0
        result = super().stats()
        result.update({
            "lists": len(sizes),
            "list_size": {"min": min(sizes) if sizes else 0, "mean": round(mean, 1), "max": max(sizes) if sizes else 0},
            "imbalance": round(max(sizes) / mean, 2) if mean else 0.0,
            "trained_on": self.trained_on,
            "growth_since_training": round(self.live / self.trained_on, 2) if self.trained_on else 0.0,
            "warnings": list(self.warnings),
        })
        return result


class HNSW(Index):
    kind = "hnsw"

    def __init__(self, space: Space, m: int = 16, ef_construction: int = 64, seed: int = 0):
        super().__init__(space)
        if m < 2:
            raise InputError("M must be at least 2")
        if ef_construction < 1:
            raise InputError("efConstruction must be at least 1")
        self.m, self.ef_construction, self.seed = m, ef_construction, seed
        self.level_mult = 1.0 / math.log(m)
        self.rng = random.Random(seed)
        self.links: List[List[List[int]]] = []
        self.entry: Optional[int] = None
        self.counter = 0

    @property
    def params(self) -> Dict[str, Any]:
        return {"m": self.m, "ef_construction": self.ef_construction, "seed": self.seed}

    @property
    def top(self) -> int:
        return len(self.links[self.entry]) - 1 if self.entry is not None else -1

    def max_links(self, layer: int) -> int:
        return 2 * self.m if layer == 0 else self.m

    def random_level(self) -> int:
        return int(-math.log(1.0 - self.rng.random()) * self.level_mult)

    def search_layer(self, query: List[float], entry: List[Scored], ef: int, layer: int, accept: Optional[Callable[[int], bool]] = None, budget: int = 0) -> List[Scored]:
        nodes, links = self.nodes, self.links
        visited = {n for _, n in entry}
        candidates = list(entry)
        heapq.heapify(candidates)
        results = [(-d, n) for d, n in entry if accept is None or accept(n)]
        heapq.heapify(results)
        while len(results) > ef:
            heapq.heappop(results)
        while candidates:
            distance, node = heapq.heappop(candidates)
            if len(results) >= ef and distance > -results[0][0]:
                break
            for other in links[node][layer]:
                if other in visited:
                    continue
                visited.add(other)
                self.counter += 1
                d = math.dist(query, nodes[other].vector)
                if len(results) < ef or d < -results[0][0]:
                    heapq.heappush(candidates, (d, other))
                    if accept is None or accept(other):
                        heapq.heappush(results, (-d, other))
                        if len(results) > ef:
                            heapq.heappop(results)
            if budget and len(visited) >= budget:
                break
        return sorted((-d, n) for d, n in results)

    def select_neighbors(self, candidates: Sequence[Scored], m: int) -> List[Scored]:
        nodes = self.nodes
        chosen: List[Scored] = []
        for distance, node in candidates:
            vector = nodes[node].vector
            if all(math.dist(vector, nodes[other].vector) > distance for _, other in chosen):
                chosen.append((distance, node))
                if len(chosen) >= m:
                    break
        return chosen

    def connect(self, node: int, new: int, layer: int) -> None:
        links = self.links[node][layer]
        links.append(new)
        limit = self.max_links(layer)
        if len(links) <= limit:
            return
        base = self.nodes[node].vector
        scored = sorted((math.dist(base, self.nodes[n].vector), n) for n in links)
        self.links[node][layer] = [n for _, n in self.select_neighbors(scored, limit)]

    def _add(self, node: int) -> None:
        query = self.nodes[node].vector
        level = self.random_level()
        self.links.append([[] for _ in range(level + 1)])
        if self.entry is None:
            self.entry = node
            return
        top = self.top
        nearest = [(math.dist(query, self.nodes[self.entry].vector), self.entry)]
        for layer in range(top, level, -1):
            nearest = self.search_layer(query, nearest, 1, layer)
        for layer in range(min(level, top), -1, -1):
            found = self.search_layer(query, nearest, self.ef_construction, layer)
            chosen = self.select_neighbors(found, self.m)
            self.links[node][layer] = [n for _, n in chosen]
            for _, other in chosen:
                self.connect(other, node, layer)
            nearest = found
        if level > top:
            self.entry = node

    def _search(self, query: List[float], k: int, flt: Dict[str, Any], mode: str, ef: int = DEFAULT_EF_SEARCH, max_visit: int = DEFAULT_MAX_VISIT, **knobs: Any) -> SearchResult:
        if self.entry is None:
            return SearchResult([], 0, mode)
        self.counter = 1
        nearest = [(math.dist(query, self.nodes[self.entry].vector), self.entry)]
        for layer in range(self.top, 0, -1):
            nearest = self.search_layer(query, nearest, 1, layer)
        if mode == "iterative":
            found = self.search_layer(query, nearest, max(ef, k), 0, self.accepts(flt), budget=max_visit)
        else:
            found = self.search_layer(query, nearest, ef, 0, self.accepts(None))
            if flt:
                found = [(d, n) for d, n in found if matches(self.nodes[n].meta, flt)]
        return SearchResult(self.hits(found[:k]), self.counter, mode if flt else "ann", f"ef_search={ef}")

    def stats(self) -> Dict[str, Any]:
        levels: Dict[int, int] = {}
        for node_links in self.links:
            levels[len(node_links) - 1] = levels.get(len(node_links) - 1, 0) + 1
        degree = [len(node_links[0]) for node_links in self.links]
        result = super().stats()
        result.update({
            "levels": {str(level): levels[level] for level in sorted(levels)},
            "entry_level": self.top,
            "layer0_degree": {"min": min(degree) if degree else 0, "mean": round(sum(degree) / len(degree), 1) if degree else 0.0, "max": max(degree) if degree else 0, "cap": 2 * self.m},
        })
        return result


def make_index(kind: str, space: Space, m: int = 16, ef_construction: int = 64, nlist: int = DEFAULT_NLIST, seed: int = 0) -> Index:
    if kind == "flat":
        return Index(space)
    if kind == "ivf":
        return IVF(space, nlist=nlist, seed=seed)
    if kind == "hnsw":
        return HNSW(space, m=m, ef_construction=ef_construction, seed=seed)
    raise InputError(f"index must be one of {', '.join(KINDS)}, got {kind!r}")


def build(kind: str, items: Sequence[Dict[str, Any]], metric: str = "l2", **params: Any) -> Index:
    if not items:
        raise InputError("no vectors to index")
    space = Space(metric, len(as_vector(items[0]["vector"])))
    index = make_index(kind, space, **params)
    for item in items:
        index.upsert(item["id"], item["vector"], item.get("meta"))
    if isinstance(index, IVF):
        index.train()
    return index


def recall_at_k(found: Sequence[str], truth: Sequence[str], k: int) -> float:
    expected = list(truth)[:k]
    if not expected:
        return 1.0
    return len(set(list(found)[:k]) & set(expected)) / len(expected)


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[max(1, math.ceil(pct / 100 * len(ordered))) - 1])


def knob_args(kind: str, value: int) -> Dict[str, int]:
    if kind == "hnsw":
        return {"ef": value}
    if kind == "ivf":
        return {"nprobe": value}
    return {}


def label_for(kind: str, value: int, mode: Optional[str] = None) -> str:
    label = "flat:exact" if kind == "flat" else f"{kind}:{KNOBS[kind]}={value}"
    return f"{label}:{mode}" if mode else label


def sweep(index: Index, queries: Sequence[Dict[str, Any]], k: int, values: Sequence[int], flt: Optional[Dict[str, Any]] = None, modes: Sequence[str] = ("post",)) -> List[Dict[str, Any]]:
    truths = [index.exact(index.space.prepare(q["vector"]), k, flt).keys for q in queries]
    rows = []
    for value in values:
        for mode in (modes if flt else [None]):
            recalls, returned, evals, latency = [], [], [], []
            for query, truth in zip(queries, truths):
                started = time.perf_counter()
                result = index.search(query["vector"], k, flt, mode or "post", **knob_args(index.kind, value))
                latency.append((time.perf_counter() - started) * 1000.0)
                recalls.append(recall_at_k(result.keys, truth, k))
                returned.append(len(result.hits))
                evals.append(result.evals)
            mean_evals = sum(evals) / len(evals)
            rows.append({
                "label": label_for(index.kind, value, mode),
                "index": index.kind,
                "value": value,
                "mode": mode or "none",
                "filter": dict(flt or {}),
                "queries": len(queries),
                "recall": round(sum(recalls) / len(recalls), 4),
                "min_recall": round(min(recalls), 4),
                "returned": round(sum(returned) / len(returned), 2),
                "evals": round(mean_evals, 1),
                "scanned": round(mean_evals / index.live, 4) if index.live else 0.0,
                "p50_ms": round(percentile(latency, 50), 3),
                "p95_ms": round(percentile(latency, 95), 3),
            })
    return rows


def memory_estimate(kind: str, n: int, dim: int, m: int = 16, nlist: int = DEFAULT_NLIST, float_bytes: int = FLOAT_BYTES, link_bytes: int = LINK_BYTES) -> Dict[str, int]:
    vectors = n * dim * float_bytes
    if kind == "flat":
        structure = 0
    elif kind == "ivf":
        structure = nlist * dim * float_bytes + n * link_bytes
    elif kind == "hnsw":
        structure = round(n * link_bytes * (2 * m + m / (m - 1)))
    else:
        raise InputError(f"index must be one of {', '.join(KINDS)}, got {kind!r}")
    return {"vectors": vectors, "structure": structure, "total": vectors + structure}


def work_estimate(kind: str, n: int, nlist: int = DEFAULT_NLIST, nprobe: int = 1) -> Optional[float]:
    if kind == "flat":
        return float(n)
    if kind == "ivf":
        return nlist + min(nprobe, nlist) * n / nlist
    return None


def latency_ms(evals: float, dim: int, bandwidth_gbps: float = DEFAULT_BANDWIDTH_GBPS, float_bytes: int = FLOAT_BYTES) -> float:
    return evals * dim * float_bytes / (bandwidth_gbps * 1e9) * 1000.0


def estimate(n: int, dim: int, m: int = 16, nlist: int = DEFAULT_NLIST, nprobe: int = 1, bandwidth_gbps: float = DEFAULT_BANDWIDTH_GBPS, hnsw_evals: Optional[float] = None) -> List[Dict[str, Any]]:
    rows = []
    for kind in KINDS:
        memory = memory_estimate(kind, n, dim, m, nlist)
        work = work_estimate(kind, n, nlist, nprobe) if kind != "hnsw" else hnsw_evals
        rows.append({
            "index": kind,
            "memory": memory,
            "evals": None if work is None else round(work, 1),
            "bytes_read": None if work is None else round(work * dim * FLOAT_BYTES),
            "latency_ms": None if work is None else round(latency_ms(work, dim, bandwidth_gbps), 3),
        })
    return rows


def snapshot(index: Index) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "format": FORMAT,
        "kind": index.kind,
        "metric": index.space.metric,
        "dim": index.space.dim,
        "params": index.params,
        "nodes": [{"key": n.key, "vector": n.vector, "meta": n.meta, "deleted": n.deleted} for n in index.nodes],
    }
    if isinstance(index, IVF):
        doc.update({"centroids": index.centroids, "lists": index.lists, "trained_on": index.trained_on, "warnings": index.warnings})
    if isinstance(index, HNSW):
        version, internal, gauss = index.rng.getstate()
        doc.update({"links": index.links, "entry": index.entry, "rng": [version, list(internal), gauss]})
    return doc


def restore(doc: Dict[str, Any]) -> Index:
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise InputError(f"not a {FORMAT} snapshot (format: {doc.get('format') if isinstance(doc, dict) else type(doc).__name__})")
    try:
        params = dict(doc.get("params") or {})
        iterations = params.pop("iterations", None)
        index = make_index(doc["kind"], Space(doc["metric"], int(doc["dim"])), **params)
        if isinstance(index, IVF) and iterations:
            index.iterations = int(iterations)
        for position, raw in enumerate(doc["nodes"]):
            node = Node(str(raw["key"]), [float(v) for v in raw["vector"]], dict(raw.get("meta") or {}), bool(raw.get("deleted")))
            if len(node.vector) != index.space.dim:
                raise InputError(f"snapshot node {position} has {len(node.vector)} dimensions, expected {index.space.dim}")
            index.nodes.append(node)
            if not node.deleted:
                index.by_key[node.key] = position
        if isinstance(index, IVF):
            index.centroids = [list(map(float, c)) for c in doc["centroids"]]
            index.lists = [list(map(int, members)) for members in doc["lists"]]
            index.trained_on = int(doc.get("trained_on") or 0)
            index.warnings = list(doc.get("warnings") or [])
        if isinstance(index, HNSW):
            index.links = [[list(map(int, layer)) for layer in node_links] for node_links in doc["links"]]
            index.entry = doc["entry"]
            if len(index.links) != len(index.nodes):
                raise InputError(f"snapshot has {len(index.links)} link lists for {len(index.nodes)} nodes")
            version, internal, gauss = doc["rng"]
            index.rng.setstate((version, tuple(internal), gauss))
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError(f"snapshot is incomplete or corrupt: {exc!r}")
    return index


def save(index: Index, path: str) -> int:
    body = json.dumps(snapshot(index), separators=(",", ":"))
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.replace(temporary, path)
    return len(body)


def load(path: str) -> Index:
    return restore(read_json(path))


def read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")
    except ValueError as exc:
        raise InputError(f"{path} is not JSON: {exc}")


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    rows = []
    try:
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    raise InputError(f"{path} line {number}: not JSON ({exc})")
                if not isinstance(row, dict) or "vector" not in row:
                    raise InputError(f"{path} line {number}: expected an object with a \"vector\" field")
                rows.append(row)
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")
    return rows


def read_items(path: str) -> List[Dict[str, Any]]:
    items = read_jsonl(path)
    for position, item in enumerate(items):
        item.setdefault("id", str(position))
        item["id"] = str(item["id"])
    return items


def read_queries(path: str) -> List[Dict[str, Any]]:
    queries = read_jsonl(path)
    for position, query in enumerate(queries):
        query.setdefault("qid", position)
    return queries


def generate(n: int, dim: int, clusters: int, queries: int, seed: int, tags: int = 10, spread: float = 0.8) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if min(n, dim, clusters, tags) < 1 or queries < 0:
        raise InputError("n, dim, clusters and tags must be at least 1, queries at least 0")
    rng = random.Random(seed)
    centres = [[rng.uniform(-1.0, 1.0) for _ in range(dim)] for _ in range(clusters)]

    def sample() -> List[float]:
        centre = centres[rng.randrange(clusters)]
        return [round(x + rng.gauss(0.0, spread), 5) for x in centre]

    items = [{"id": str(i), "vector": sample(), "meta": {"tag": i % tags}} for i in range(n)]
    probes = [{"qid": i, "vector": sample()} for i in range(queries)]
    return items, probes


def copy_field(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def pg_vector(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def pgcopy_lines(items: Iterable[Dict[str, Any]]) -> List[str]:
    return ["\t".join([copy_field(str(item["id"])), copy_field(json.dumps(item.get("meta") or {}, sort_keys=True)), pg_vector(item["vector"])]) for item in items]


def sql_text(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def pg_query(label: str, query: Dict[str, Any], k: int, metric: str, flt: Optional[Dict[str, Any]] = None, table: str = "items") -> str:
    filter_text = json.dumps(flt or {}, sort_keys=True)
    where = f" WHERE meta @> {sql_text(filter_text)}::jsonb" if flt else ""
    inner = f"SELECT id, embedding {PG_OPERATORS[metric]} {sql_text(pg_vector(query['vector']))} AS d FROM {table}{where} ORDER BY d LIMIT {k}"
    return f"SELECT {sql_text(label)}, {sql_text(filter_text)}, {int(query['qid'])}, coalesce(string_agg(id, ',' ORDER BY d), '') FROM ({inner}) AS r;"


def benchmark_sql(queries: Sequence[Dict[str, Any]], k: int, metric: str, ef_values: Sequence[int], probe_values: Sequence[int], m: int = 16, ef_construction: int = 64, lists: int = DEFAULT_NLIST, flt: Optional[Dict[str, Any]] = None, filter_ef: int = DEFAULT_EF_SEARCH, filter_probes: int = 1, table: str = "items") -> str:
    if metric not in METRICS:
        raise InputError(f"metric must be one of {', '.join(METRICS)}, got {metric!r}")
    opclass = PG_OPCLASSES[metric]
    lines = [
        "-- generated by vecindex sql; run: psql -qAt -F $'\\t' -v ON_ERROR_STOP=1 -f bench.sql > results.tsv",
        f"DROP INDEX IF EXISTS {PG_INDEX};",
        "RESET enable_seqscan;",
    ]

    def run(label: str, where: Optional[Dict[str, Any]] = None) -> None:
        lines.extend(pg_query(label, q, k, metric, where, table) for q in queries)

    run("exact")
    if ef_values:
        lines.append(f"CREATE INDEX {PG_INDEX} ON {table} USING hnsw (embedding {opclass}) WITH (m = {m}, ef_construction = {ef_construction});")
        lines.append("SET enable_seqscan = off;")
        for ef in ef_values:
            lines.append(f"SET hnsw.ef_search = {int(ef)};")
            run(label_for("hnsw", int(ef)))
        if flt:
            lines.append(f"SET hnsw.ef_search = {int(filter_ef)};")
            lines.append("SET hnsw.iterative_scan = off;")
            run(label_for("hnsw", int(filter_ef), "post"), flt)
            lines.append("SET hnsw.iterative_scan = relaxed_order;")
            run(label_for("hnsw", int(filter_ef), "iterative"), flt)
            lines.append("RESET hnsw.iterative_scan;")
        lines.append(f"DROP INDEX {PG_INDEX};")
        lines.append("RESET enable_seqscan;")
    if probe_values:
        lines.append(f"CREATE INDEX {PG_INDEX} ON {table} USING ivfflat (embedding {opclass}) WITH (lists = {lists});")
        lines.append("SET enable_seqscan = off;")
        for probes in probe_values:
            lines.append(f"SET ivfflat.probes = {int(probes)};")
            run(label_for("ivf", int(probes)))
        if flt:
            lines.append(f"SET ivfflat.probes = {int(filter_probes)};")
            lines.append("SET ivfflat.iterative_scan = off;")
            run(label_for("ivf", int(filter_probes), "post"), flt)
            lines.append("SET ivfflat.iterative_scan = relaxed_order;")
            run(label_for("ivf", int(filter_probes), "iterative"), flt)
            lines.append("RESET ivfflat.iterative_scan;")
        lines.append(f"DROP INDEX {PG_INDEX};")
        lines.append("RESET enable_seqscan;")
    return "\n".join(lines) + "\n"


def parse_results(text: str) -> List[Tuple[str, Dict[str, Any], int, List[str]]]:
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            raise InputError(f"results line {number}: expected label, filter, query id and ids separated by tabs, got {len(parts)} field(s): {line[:80]!r}")
        label, filter_text, qid, ids = parts
        try:
            flt = json.loads(filter_text)
            query_id = int(qid)
        except ValueError:
            raise InputError(f"results line {number}: filter must be JSON and the query id an integer: {line[:80]!r}")
        rows.append((label, flt if isinstance(flt, dict) else {}, query_id, [x for x in ids.split(",") if x]))
    return rows


def grade(rows: Sequence[Tuple[str, Dict[str, Any], int, List[str]]], reference: Index, queries: Sequence[Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
    by_qid = {int(q["qid"]): q for q in queries}
    truths: Dict[Tuple[int, str], List[str]] = {}
    groups: Dict[str, Dict[str, Any]] = {}
    for label, flt, qid, ids in rows:
        if qid not in by_qid:
            raise InputError(f"results mention query {qid}, which is not in the queries file")
        key = (qid, json.dumps(flt, sort_keys=True))
        if key not in truths:
            truths[key] = reference.exact(reference.space.prepare(by_qid[qid]["vector"]), k, flt).keys
        group = groups.setdefault(label, {"label": label, "filter": flt, "recalls": [], "returned": []})
        group["recalls"].append(recall_at_k(ids, truths[key], k))
        group["returned"].append(len(ids))
    return [
        {
            "label": g["label"],
            "filter": g["filter"],
            "queries": len(g["recalls"]),
            "recall": round(sum(g["recalls"]) / len(g["recalls"]), 4),
            "min_recall": round(min(g["recalls"]), 4),
            "returned": round(sum(g["returned"]) / len(g["returned"]), 2),
        }
        for g in groups.values()
    ]


def human_bytes(count: float) -> str:
    for unit_name in ("B", "KiB", "MiB", "GiB"):
        if abs(count) < 1024 or unit_name == "GiB":
            return f"{count:.0f} {unit_name}" if unit_name == "B" else f"{count:.1f} {unit_name}"
        count /= 1024
    return f"{count:.1f} GiB"


def render_rows(rows: Sequence[Dict[str, Any]], k: int) -> str:
    width = max([len(r["label"]) for r in rows] + [5])
    lines = [f"  {'label':<{width}}  {'recall@' + str(k):>9}  {'min':>5}  {'returned':>8}" + ("  {:>8}  {:>7}  {:>8}".format("evals", "scanned", "p95 ms") if rows and "evals" in rows[0] else "")]
    for r in rows:
        line = f"  {r['label']:<{width}}  {r['recall']:>9.3f}  {r['min_recall']:>5.2f}  {r['returned']:>8.1f}"
        if "evals" in r:
            line += f"  {r['evals']:>8.0f}  {r['scanned']:>6.1%}  {r['p95_ms']:>8.2f}"
        lines.append(line)
    return "\n".join(lines)


def render_hits(index: Index, result: SearchResult, k: int) -> str:
    lines = [f"{index.kind}  {index.space.metric}  {index.live} live, {index.tombstones} tombstones  mode {result.mode}  {result.note}".rstrip()]
    for rank, hit in enumerate(result.hits, 1):
        lines.append(f"  {rank:>3}  {hit.key:<12} {hit.distance:.5f}  {json.dumps(hit.meta, sort_keys=True)}")
    if len(result.hits) < k:
        lines.append(f"  returned {len(result.hits)} of k={k}")
    share = result.evals / index.live if index.live else 0.0
    lines.append(f"distance evaluations {result.evals} ({share:.1%} of live vectors)")
    return "\n".join(lines)


def render_estimate(rows: Sequence[Dict[str, Any]], n: int, dim: int, bandwidth: float) -> str:
    lines = [f"n={n} dim={dim}: raw vectors {human_bytes(n * dim * FLOAT_BYTES)} at float32; latency assumes {bandwidth:g} GB/s of vector reads per query"]
    for r in rows:
        memory = r["memory"]
        work = "measure with sweep (--hnsw-evals)" if r["evals"] is None else f"{r['evals']:.0f} distances, {human_bytes(r['bytes_read'])} read, ~{r['latency_ms']:.3f} ms"
        lines.append(f"  {r['index']:<5} memory {human_bytes(memory['total']):>10}  structure {human_bytes(memory['structure']):>10}  per query: {work}")
    return "\n".join(lines)


def int_list(text: str) -> List[int]:
    try:
        values = [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise InputError(f"expected a comma-separated list of integers, got {text!r}")
    if not values or min(values) < 1:
        raise InputError(f"expected positive integers, got {text!r}")
    return values


def add_index_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--index", choices=KINDS, default="hnsw", help="flat (exact), ivf or hnsw (default hnsw)")
    parser.add_argument("--metric", choices=METRICS, default="l2", help="l2 (pgvector <->) or cosine (<=>)")
    parser.add_argument("--m", type=int, default=16, help="HNSW M: links per node per layer, 2*M on layer 0 (default 16)")
    parser.add_argument("--ef-construction", type=int, default=64, help="HNSW candidate list size while inserting (default 64)")
    parser.add_argument("--nlist", type=int, default=DEFAULT_NLIST, help=f"IVF number of lists, the k-means centroids (default {DEFAULT_NLIST})")
    parser.add_argument("--seed", type=int, default=0, help="seed for HNSW levels and k-means")


def index_params(args: argparse.Namespace) -> Dict[str, Any]:
    return {"m": args.m, "ef_construction": args.ef_construction, "nlist": args.nlist, "seed": args.seed}


def emit(payload: Any, text: str, as_json: bool) -> None:
    print(json.dumps(payload, indent=2) if as_json else text)


def cmd_generate(args: argparse.Namespace) -> int:
    items, queries = generate(args.n, args.dim, args.clusters, args.queries, args.seed, args.tags, args.spread)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.writelines(json.dumps(item) + "\n" for item in items)
    if args.queries_out:
        with open(args.queries_out, "w", encoding="utf-8") as handle:
            handle.writelines(json.dumps(query) + "\n" for query in queries)
    if args.pgcopy:
        with open(args.pgcopy, "w", encoding="utf-8") as handle:
            handle.writelines(line + "\n" for line in pgcopy_lines(items))
    print(f"wrote {len(items)} vectors ({args.dim} dims, {args.clusters} clusters, tag = id % {args.tags}) to {args.out}" + (f", {len(queries)} queries to {args.queries_out}" if args.queries_out else "") + (f", COPY rows to {args.pgcopy}" if args.pgcopy else ""))
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    index = build(args.index, read_items(args.data), args.metric, **index_params(args))
    seconds = time.perf_counter() - started
    size = save(index, args.output)
    stats = index.stats()
    stats.update({"build_seconds": round(seconds, 2), "snapshot_bytes": size, "memory_estimate": memory_estimate(index.kind, index.live, index.space.dim, args.m, args.nlist)})
    text = f"built {index.kind} over {index.live} vectors ({index.space.dim} dims, {index.space.metric}) in {seconds:.1f} s; snapshot {args.output} ({human_bytes(size)})\n" + json.dumps({k: v for k, v in stats.items() if k not in ("kind", "metric", "dim")}, sort_keys=True)
    emit(stats, text, args.json)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    index = load(args.index_file)
    if args.vector is None and args.query_file is None:
        raise InputError("give --vector '[...]' or --query-file queries.jsonl")
    vectors = [as_vector(args.vector)] if args.vector is not None else [q["vector"] for q in read_queries(args.query_file)][: args.limit]
    flt = parse_filter(args.filter)
    results = [index.search(v, args.k, flt, args.filter_mode, ef=args.ef, nprobe=args.nprobe, max_visit=args.max_visit, max_probes=args.max_probes) for v in vectors]
    payload = [{"mode": r.mode, "evals": r.evals, "note": r.note, "hits": [{"id": h.key, "distance": h.distance, "meta": h.meta} for h in r.hits]} for r in results]
    emit(payload if len(payload) > 1 else payload[0], "\n".join(render_hits(index, r, args.k) for r in results), args.json)
    return 0


def cmd_upsert(args: argparse.Namespace) -> int:
    index = load(args.index_file)
    try:
        meta = json.loads(args.meta) if args.meta else {}
    except ValueError:
        raise InputError(f"--meta must be a JSON object, got {args.meta!r}")
    replaced = args.id in index.by_key
    index.upsert(args.id, as_vector(args.vector), meta)
    save(index, args.index_file)
    emit({"id": args.id, "replaced": replaced, "live": index.live, "tombstones": index.tombstones}, f"{'replaced' if replaced else 'inserted'} {args.id}: {index.live} live, {index.tombstones} tombstones", args.json)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    index = load(args.index_file)
    if not index.delete(args.id):
        raise InputError(f"no live vector with id {args.id!r}")
    save(index, args.index_file)
    emit({"id": args.id, "live": index.live, "tombstones": index.tombstones}, f"tombstoned {args.id}: {index.live} live, {index.tombstones} tombstones (rebuild to reclaim them)", args.json)
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    if args.snapshot:
        index = load(args.snapshot)
    elif args.data:
        index = build(args.index, read_items(args.data), args.metric, **index_params(args))
    else:
        raise InputError("give --snapshot index.json or --data data.jsonl")
    queries = read_queries(args.queries)
    if not queries:
        raise InputError(f"{args.queries} has no queries")
    flt = parse_filter(args.filter)
    modes = [m.strip() for m in args.filter_modes.split(",") if m.strip()]
    for mode in modes:
        if mode not in FILTER_MODES:
            raise InputError(f"filter mode must be one of {', '.join(FILTER_MODES)}, got {mode!r}")
    rows = sweep(index, queries, args.k, int_list(args.values) if args.values else DEFAULT_VALUES[index.kind], flt, modes)
    payload = {"index": index.kind, "params": index.params, "metric": index.space.metric, "live": index.live, "k": args.k, "rows": rows}
    header = f"{index.kind} {json.dumps(index.params, sort_keys=True)} over {index.live} vectors, {len(queries)} queries" + (f", filter {json.dumps(flt, sort_keys=True)}" if flt else "")
    emit(payload, header + "\n" + render_rows(rows, args.k), args.json)
    return gate(rows, args.min_recall)


def gate(rows: Sequence[Dict[str, Any]], minimum: Optional[float]) -> int:
    if minimum is None:
        return 0
    failing = [r["label"] for r in rows if r["recall"] < minimum]
    if failing:
        print(f"recall below {minimum}: {', '.join(failing)}", file=sys.stderr)
        return 1
    return 0


def cmd_sql(args: argparse.Namespace) -> int:
    queries = read_queries(args.queries)[: args.limit]
    text = benchmark_sql(queries, args.k, args.metric, int_list(args.hnsw_ef) if args.hnsw_ef else [], int_list(args.ivf_probes) if args.ivf_probes else [], args.m, args.ef_construction, args.lists, parse_filter(args.filter), args.filter_ef, args.filter_probes, args.table)
    sys.stdout.write(text)
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    reference = build("flat", read_items(args.data), args.metric)
    try:
        with open(args.results, encoding="utf-8") as handle:
            rows = parse_results(handle.read())
    except OSError as exc:
        raise InputError(f"cannot read {args.results}: {exc.strerror}")
    if not rows:
        raise InputError(f"{args.results} has no result rows; did psql print anything?")
    graded = grade(rows, reference, read_queries(args.queries), args.k)
    emit({"k": args.k, "rows": graded}, f"graded against exact search over {reference.live} vectors\n" + render_rows(graded, args.k), args.json)
    return gate(graded, args.min_recall)


def cmd_estimate(args: argparse.Namespace) -> int:
    rows = estimate(args.n, args.dim, args.m, args.nlist, args.nprobe, args.bandwidth_gbps, args.hnsw_evals)
    emit({"n": args.n, "dim": args.dim, "bandwidth_gbps": args.bandwidth_gbps, "rows": rows}, render_estimate(rows, args.n, args.dim, args.bandwidth_gbps), args.json)
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="vecindex", description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate", help="seeded clustered vectors and queries (+ COPY rows for pgvector)")
    p.add_argument("--n", type=int, default=4000)
    p.add_argument("--dim", type=int, default=32)
    p.add_argument("--clusters", type=int, default=50)
    p.add_argument("--queries", type=int, default=50)
    p.add_argument("--tags", type=int, default=10, help="metadata tag = id %% tags, so each tag matches 1/tags of the rows")
    p.add_argument("--spread", type=float, default=0.8, help="standard deviation around each cluster centre; larger means overlapping clusters and harder search")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", required=True, help="vectors as JSON lines: {\"id\", \"vector\", \"meta\"}")
    p.add_argument("--queries-out", help="queries as JSON lines: {\"qid\", \"vector\"}")
    p.add_argument("--pgcopy", help="also write tab-separated rows for COPY items (id, meta, embedding) FROM STDIN")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("build", help="build an index from JSON lines and save a snapshot")
    p.add_argument("--data", required=True)
    add_index_options(p)
    p.add_argument("-o", "--output", required=True, help="snapshot file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("search", help="k nearest neighbours from a snapshot")
    p.add_argument("--index", dest="index_file", required=True, help="snapshot file")
    p.add_argument("--vector", help="query vector, e.g. '[0.1,0.2]'")
    p.add_argument("--query-file", help="JSON lines of queries")
    p.add_argument("--limit", type=int, default=5, help="queries to run from --query-file")
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--ef", type=int, default=DEFAULT_EF_SEARCH, help="HNSW ef_search (pgvector hnsw.ef_search, default 40)")
    p.add_argument("--nprobe", type=int, default=1, help="IVF lists to probe (pgvector ivfflat.probes, default 1)")
    p.add_argument("--max-visit", type=int, default=DEFAULT_MAX_VISIT, help="iterative HNSW: stop after visiting this many nodes (like hnsw.max_scan_tuples)")
    p.add_argument("--max-probes", type=int, help="iterative IVF: probe at most this many lists (like ivfflat.max_probes)")
    p.add_argument("--filter", help="metadata equality filter, e.g. tag=3")
    p.add_argument("--filter-mode", choices=FILTER_MODES, default="post")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("upsert", help="insert or replace one vector in a snapshot (the old version becomes a tombstone)")
    p.add_argument("--index", dest="index_file", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--vector", required=True)
    p.add_argument("--meta", help="JSON object")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_upsert)

    p = sub.add_parser("delete", help="tombstone one vector in a snapshot")
    p.add_argument("--index", dest="index_file", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("sweep", help="recall@k, work and latency across ef_search or probes, against exact search")
    p.add_argument("--snapshot", help="load this snapshot instead of building")
    p.add_argument("--data", help="build from these JSON lines")
    add_index_options(p)
    p.add_argument("--queries", required=True)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--values", help="ef_search values for hnsw (default 10,20,40,80,200) or probes for ivf (default 1,2,4,8,16)")
    p.add_argument("--filter", help="metadata equality filter, e.g. tag=3")
    p.add_argument("--filter-modes", default="post,iterative,pre")
    p.add_argument("--min-recall", type=float, help="exit 1 if any row is below this recall")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("sql", help="the same benchmark as a psql script for pgvector")
    p.add_argument("--queries", required=True)
    p.add_argument("--limit", type=int, default=1000, help="queries to include")
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--metric", choices=METRICS, default="l2")
    p.add_argument("--hnsw-ef", default="10,20,40,80,200", help="hnsw.ef_search values; empty to skip HNSW")
    p.add_argument("--ivf-probes", default="1,2,4,8,16", help="ivfflat.probes values; empty to skip IVFFlat")
    p.add_argument("--m", type=int, default=16)
    p.add_argument("--ef-construction", type=int, default=64)
    p.add_argument("--lists", type=int, default=DEFAULT_NLIST)
    p.add_argument("--filter", help="add filtered runs with iterative scans off and on, e.g. tag=3")
    p.add_argument("--filter-ef", type=int, default=DEFAULT_EF_SEARCH)
    p.add_argument("--filter-probes", type=int, default=1)
    p.add_argument("--table", default="items")
    p.set_defaults(func=cmd_sql)

    p = sub.add_parser("recall", help="grade another system's results (label, filter, qid, ids) against exact search")
    p.add_argument("--data", required=True)
    p.add_argument("--queries", required=True)
    p.add_argument("--results", required=True, help="tab-separated lines as printed by the sql script")
    p.add_argument("--metric", choices=METRICS, default="l2")
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--min-recall", type=float, help="exit 1 if any label is below this recall")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("estimate", help="memory and per-query work for flat, IVF and HNSW at a given size")
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--dim", type=int, required=True)
    p.add_argument("--m", type=int, default=16)
    p.add_argument("--nlist", type=int, default=1000)
    p.add_argument("--nprobe", type=int, default=10)
    p.add_argument("--hnsw-evals", type=float, help="distance evaluations per query measured with sweep")
    p.add_argument("--bandwidth-gbps", type=float, default=DEFAULT_BANDWIDTH_GBPS, help="bytes of vectors one query can stream per second, in GB/s (default 10)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_estimate)
    return root


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return args.func(args)
    except InputError as exc:
        print(f"vecindex: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"vecindex: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
