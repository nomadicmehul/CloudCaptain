"""minilb: an HTTP load balancer from the standard library. Pluggable algorithms (round robin, nginx's smooth weighted round robin, least connections, power of two choices, ring hash, Maglev, modulo hash), active health checks with rise and fall, passive outlier ejection with a growing ejection time, retries limited to idempotent requests and a retry budget, connection draining, sticky cookies, and a simulator that shows what each choice does to tail latency."""
from __future__ import annotations

import argparse
import bisect
import collections
import hashlib
import heapq
import http.client
import itertools
import json
import math
import random
import socket
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ALGORITHMS = ("round_robin", "weighted_round_robin", "least_conn", "p2c", "ring_hash", "maglev", "modulo_hash")
HASH_ALGORITHMS = ("ring_hash", "maglev", "modulo_hash")
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"})
RETRY_CONDITIONS = ("connect-failure", "gateway-error", "5xx")
GATEWAY_ERRORS = frozenset({502, 503, 504})
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade", "content-length"})
HASH_SOURCES = ("header", "cookie", "path", "source")
RING_POINTS_PER_WEIGHT = 160
MAGLEV_TABLE_SIZE = 65537
ADMIN_PREFIX = "/_minilb/"
MAX_EVENTS = 200
DEFAULT_SIM_BACKENDS = ("fast1=20", "fast2=20", "slow=100")


class InputError(Exception):
    pass


class UnknownBackend(InputError):
    pass


@dataclass
class Backend:
    name: str
    address: str = ""
    weight: int = 1
    healthy: bool = True
    draining: bool = False
    drain_deadline: float = 0.0
    active: int = 0
    requests: int = 0
    failures: int = 0
    passes: int = 0
    fails: int = 0
    consecutive_5xx: int = 0
    ejections: int = 0
    ejected_until: float = 0.0
    last_check: str = ""

    def ejected(self, now: float) -> bool:
        return now < self.ejected_until

    def available(self, now: float) -> bool:
        return self.healthy and not self.draining and not self.ejected(now)

    def state(self, now: float) -> str:
        if self.draining:
            return "draining"
        if not self.healthy:
            return "unhealthy"
        if self.ejected(now):
            return "ejected"
        return "healthy"


def hash64(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def is_prime(number: int) -> bool:
    if number < 2:
        return False
    for divisor in range(2, int(math.isqrt(number)) + 1):
        if number % divisor == 0:
            return False
    return True


def signature(candidates: Sequence[Backend]) -> Tuple[Tuple[str, int], ...]:
    return tuple(sorted((b.name, b.weight) for b in candidates))


def by_name(candidates: Sequence[Backend], name: str) -> Backend:
    for backend in candidates:
        if backend.name == name:
            return backend
    raise UnknownBackend(f"no backend named {name!r}")


class RoundRobin:
    name = "round_robin"

    def __init__(self) -> None:
        self.counter = 0

    def pick(self, candidates: Sequence[Backend], key: Optional[str] = None) -> Backend:
        backend = candidates[self.counter % len(candidates)]
        self.counter += 1
        return backend


class SmoothWeightedRoundRobin:
    name = "weighted_round_robin"

    def __init__(self) -> None:
        self.current: Dict[str, int] = {}

    def pick(self, candidates: Sequence[Backend], key: Optional[str] = None) -> Backend:
        total = 0
        best = candidates[0]
        for backend in candidates:
            self.current[backend.name] = self.current.get(backend.name, 0) + backend.weight
            total += backend.weight
            if self.current[backend.name] > self.current[best.name]:
                best = backend
        self.current[best.name] -= total
        return best


class LeastConnections:
    name = "least_conn"

    def __init__(self) -> None:
        self.counter = 0

    def pick(self, candidates: Sequence[Backend], key: Optional[str] = None) -> Backend:
        lowest = min(Fraction(b.active, b.weight) for b in candidates)
        tied = [b for b in candidates if Fraction(b.active, b.weight) == lowest]
        backend = tied[self.counter % len(tied)]
        self.counter += 1
        return backend


class PowerOfTwoChoices:
    name = "p2c"

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def pick(self, candidates: Sequence[Backend], key: Optional[str] = None) -> Backend:
        if len(candidates) == 1:
            return candidates[0]
        first, second = self.rng.sample(list(candidates), 2)
        return first if first.active * second.weight <= second.active * first.weight else second


class ModuloHash:
    name = "modulo_hash"

    def __init__(self) -> None:
        self.fallback = RoundRobin()

    def pick(self, candidates: Sequence[Backend], key: Optional[str] = None) -> Backend:
        if key is None:
            return self.fallback.pick(candidates)
        slots = [b for b in sorted(candidates, key=lambda b: b.name) for _ in range(b.weight)]
        return slots[hash64(key) % len(slots)]


class RingHash:
    name = "ring_hash"

    def __init__(self, points_per_weight: int = RING_POINTS_PER_WEIGHT) -> None:
        self.points_per_weight = points_per_weight
        self.fallback = RoundRobin()
        self.cache: Dict[Tuple[Tuple[str, int], ...], Tuple[List[int], List[str]]] = {}

    def ring(self, candidates: Sequence[Backend]) -> Tuple[List[int], List[str]]:
        sig = signature(candidates)
        if sig not in self.cache:
            if len(self.cache) > 32:
                self.cache.clear()
            points = sorted((hash64(f"{name}-{index}"), name) for name, weight in sig for index in range(weight * self.points_per_weight))
            self.cache[sig] = ([p[0] for p in points], [p[1] for p in points])
        return self.cache[sig]

    def owner(self, candidates: Sequence[Backend], key: str) -> str:
        hashes, owners = self.ring(candidates)
        index = bisect.bisect_left(hashes, hash64(key))
        return owners[index % len(owners)]

    def pick(self, candidates: Sequence[Backend], key: Optional[str] = None) -> Backend:
        if key is None:
            return self.fallback.pick(candidates)
        return by_name(candidates, self.owner(candidates, key))


def maglev_table(names: Sequence[str], weights: Sequence[int], size: int) -> List[str]:
    offsets = [hash64(f"offset:{name}") % size for name in names]
    skips = [hash64(f"skip:{name}") % (size - 1) + 1 for name in names]
    following = [0] * len(names)
    entries = [-1] * size
    filled = 0
    while True:
        for index in range(len(names)):
            for _ in range(weights[index]):
                slot = (offsets[index] + following[index] * skips[index]) % size
                while entries[slot] >= 0:
                    following[index] += 1
                    slot = (offsets[index] + following[index] * skips[index]) % size
                entries[slot] = index
                following[index] += 1
                filled += 1
                if filled == size:
                    return [names[e] for e in entries]


class Maglev:
    name = "maglev"

    def __init__(self, table_size: int = MAGLEV_TABLE_SIZE) -> None:
        if not is_prime(table_size):
            raise InputError(f"Maglev table size {table_size} must be a prime (Envoy's default is {MAGLEV_TABLE_SIZE})")
        self.size = table_size
        self.fallback = RoundRobin()
        self.cache: Dict[Tuple[Tuple[str, int], ...], List[str]] = {}

    def table(self, candidates: Sequence[Backend]) -> List[str]:
        sig = signature(candidates)
        if sig not in self.cache:
            if len(self.cache) > 32:
                self.cache.clear()
            self.cache[sig] = maglev_table([s[0] for s in sig], [s[1] for s in sig], self.size)
        return self.cache[sig]

    def pick(self, candidates: Sequence[Backend], key: Optional[str] = None) -> Backend:
        if key is None:
            return self.fallback.pick(candidates)
        return by_name(candidates, self.table(candidates)[hash64(key) % self.size])


def make_algorithm(name: str, rng: Optional[random.Random] = None, table_size: int = MAGLEV_TABLE_SIZE) -> Any:
    if name == "round_robin":
        return RoundRobin()
    if name == "weighted_round_robin":
        return SmoothWeightedRoundRobin()
    if name == "least_conn":
        return LeastConnections()
    if name == "p2c":
        return PowerOfTwoChoices(rng or random.Random())
    if name == "ring_hash":
        return RingHash()
    if name == "maglev":
        return Maglev(table_size)
    if name == "modulo_hash":
        return ModuloHash()
    raise InputError(f"unknown algorithm {name!r} (choose from {', '.join(ALGORITHMS)})")


@dataclass
class HealthPolicy:
    path: str = "/health"
    interval: float = 2.0
    timeout: float = 1.0
    rise: int = 2
    fall: int = 3
    enabled: bool = True


@dataclass
class OutlierPolicy:
    consecutive_5xx: int = 5
    base_ejection: float = 30.0
    max_ejection: float = 300.0
    max_ejection_percent: int = 10
    always_eject_one_host: bool = False


@dataclass
class RetryPolicy:
    retries: int = 1
    retry_on: Tuple[str, ...] = ("connect-failure", "gateway-error")
    budget_percent: float = 20.0
    min_retry_concurrency: int = 3


@dataclass
class RetryBudget:
    percent: float = 20.0
    minimum: int = 3
    active_requests: int = 0
    active_retries: int = 0
    refused: int = 0

    def limit(self) -> float:
        return max(float(self.minimum), self.active_requests * self.percent / 100.0)

    def allows(self) -> bool:
        return self.active_retries < self.limit()


def record_check(backend: Backend, ok: bool, policy: HealthPolicy) -> Optional[str]:
    if ok:
        backend.fails = 0
        backend.passes += 1
        if not backend.healthy and backend.passes >= policy.rise:
            backend.healthy = True
            return "up"
        return None
    backend.passes = 0
    backend.fails += 1
    if backend.healthy and backend.fails >= policy.fall:
        backend.healthy = False
        return "down"
    return None


def may_eject(ejected_now: int, pool_size: int, policy: OutlierPolicy) -> bool:
    if policy.always_eject_one_host and ejected_now == 0:
        return True
    return (ejected_now + 1) * 100 <= pool_size * policy.max_ejection_percent


def ejection_time(policy: OutlierPolicy, ejections: int) -> float:
    return min(policy.base_ejection * ejections, max(policy.base_ejection, policy.max_ejection))


def record_response(backend: Backend, status: int, now: float, policy: OutlierPolicy, ejected_now: int, pool_size: int) -> Optional[Tuple[str, float]]:
    if status < 500:
        backend.consecutive_5xx = 0
        return None
    backend.consecutive_5xx += 1
    if policy.consecutive_5xx <= 0 or backend.consecutive_5xx < policy.consecutive_5xx or backend.ejected(now):
        return None
    if not may_eject(ejected_now, pool_size, policy):
        return "refused", 0.0
    backend.ejections += 1
    backend.consecutive_5xx = 0
    duration = ejection_time(policy, backend.ejections)
    backend.ejected_until = now + duration
    return "ejected", duration


def retry_reason(method: str, outcome: str, status: int, policy: RetryPolicy) -> Optional[str]:
    if outcome == "connect-failure":
        return "connect-failure" if "connect-failure" in policy.retry_on else None
    if method.upper() not in IDEMPOTENT_METHODS:
        return None
    if status in GATEWAY_ERRORS and "gateway-error" in policy.retry_on:
        return "gateway-error"
    if status >= 500 and "5xx" in policy.retry_on:
        return "5xx"
    return None


def cookie_value(header: Optional[str], name: str) -> Optional[str]:
    for part in (header or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value.strip().strip('"') or None
    return None


def parse_hash_on(spec: str) -> Tuple[str, str]:
    source, _, name = spec.partition(":")
    source = source.strip().lower()
    if source not in HASH_SOURCES:
        raise InputError(f"--hash-on {spec!r}: expected header:NAME, cookie:NAME, path or source")
    if source in ("header", "cookie") and not name.strip():
        raise InputError(f"--hash-on {spec!r}: {source} needs a name, for example {source}:X-User")
    return source, name.strip().lower() if source == "header" else name.strip()


def hash_key(hash_on: Optional[Tuple[str, str]], headers: Dict[str, str], path: str, client: str) -> Optional[str]:
    if hash_on is None:
        return None
    source, name = hash_on
    if source == "header":
        return headers.get(name) or None
    if source == "cookie":
        return cookie_value(headers.get("cookie"), name)
    if source == "path":
        return urllib.parse.urlsplit(path).path
    return client


class Pool:
    def __init__(self, backends: Sequence[Backend], algorithm: Any, health: Optional[HealthPolicy] = None, outlier: Optional[OutlierPolicy] = None,
                 retry: Optional[RetryPolicy] = None, sticky_cookie: Optional[str] = None, clock: Callable[[], float] = time.monotonic):
        self.backends: List[Backend] = list(backends)
        self.algorithm = algorithm
        self.health = health or HealthPolicy(enabled=False)
        self.outlier = outlier or OutlierPolicy(consecutive_5xx=0)
        self.retry = retry or RetryPolicy(retries=0)
        self.budget = RetryBudget(self.retry.budget_percent, self.retry.min_retry_concurrency)
        self.sticky_cookie = sticky_cookie
        self.clock = clock
        self.started = clock()
        self.lock = threading.RLock()
        self.events: List[Dict[str, Any]] = []
        self.refused_ejections = 0
        self.listeners: List[Callable[[Dict[str, Any]], None]] = []

    def log(self, backend: str, event: str, reason: str) -> None:
        entry = {"t": round(self.clock() - self.started, 3), "backend": backend, "event": event, "reason": reason}
        self.events.append(entry)
        del self.events[:-MAX_EVENTS]
        for listener in self.listeners:
            listener(entry)

    def find(self, name: str) -> Optional[Backend]:
        for backend in self.backends:
            if backend.name == name:
                return backend
        return None

    def candidates(self, exclude: Sequence[str] = ()) -> List[Backend]:
        now = self.clock()
        return [b for b in self.backends if b.available(now) and b.name not in exclude]

    def acquire(self, key: Optional[str] = None, sticky: Optional[str] = None, exclude: Sequence[str] = ()) -> Tuple[Optional[Backend], str]:
        with self.lock:
            self.reap()
            candidates = self.candidates(exclude)
            if not candidates:
                return None, "no healthy upstream"
            chosen = next((b for b in candidates if b.name == sticky), None) if sticky else None
            how = "sticky" if chosen else self.algorithm.name
            if chosen is None:
                chosen = self.algorithm.pick(candidates, key)
            chosen.active += 1
            chosen.requests += 1
            return chosen, how

    def release(self, backend: Backend, status: int) -> None:
        with self.lock:
            backend.active -= 1
            if status >= 500:
                backend.failures += 1
            now = self.clock()
            ejected_now = sum(1 for b in self.backends if b.ejected(now))
            result = record_response(backend, status, now, self.outlier, ejected_now, len(self.backends))
            if result and result[0] == "ejected":
                self.log(backend.name, "ejected", f"{self.outlier.consecutive_5xx} consecutive 5xx; ejected for {result[1]:g}s (ejection {backend.ejections})")
            elif result:
                self.refused_ejections += 1
                self.log(backend.name, "ejection-refused", f"ejecting would make {ejected_now + 1} of {len(self.backends)} ejected, over max_ejection_percent {self.outlier.max_ejection_percent}")
            self.reap()

    def record_check(self, backend: Backend, ok: bool, detail: str) -> Optional[str]:
        with self.lock:
            backend.last_check = detail
            transition = record_check(backend, ok, self.health)
            if transition == "down":
                self.log(backend.name, "down", f"{self.health.fall} consecutive failed checks (last: {detail})")
            elif transition == "up":
                self.log(backend.name, "up", f"{self.health.rise} consecutive passed checks")
            return transition

    def check_targets(self) -> List[Backend]:
        with self.lock:
            return [b for b in self.backends if not b.draining]

    def add(self, backend: Backend) -> None:
        with self.lock:
            if self.find(backend.name):
                raise InputError(f"backend {backend.name} already exists")
            self.backends.append(backend)
            self.log(backend.name, "added", f"{backend.address or 'simulated'} weight {backend.weight}")

    def drain(self, name: str, timeout: float) -> Backend:
        with self.lock:
            backend = self.find(name)
            if backend is None:
                raise UnknownBackend(f"no backend named {name!r} (backends: {', '.join(b.name for b in self.backends)})")
            if not backend.draining:
                backend.draining = True
                backend.drain_deadline = self.clock() + timeout
                self.log(name, "draining", f"no new requests; {backend.active} in flight; deadline {timeout:g}s")
            self.reap()
            return backend

    def reap(self) -> None:
        with self.lock:
            now = self.clock()
            for backend in list(self.backends):
                if not backend.draining:
                    continue
                if backend.active <= 0:
                    self.backends.remove(backend)
                    self.log(backend.name, "removed", "drained: no requests in flight")
                elif now >= backend.drain_deadline:
                    self.backends.remove(backend)
                    self.log(backend.name, "removed", f"drain deadline passed with {backend.active} in flight")

    def begin_request(self) -> None:
        with self.lock:
            self.budget.active_requests += 1

    def end_request(self) -> None:
        with self.lock:
            self.budget.active_requests -= 1

    def begin_retry(self) -> bool:
        with self.lock:
            if not self.budget.allows():
                self.budget.refused += 1
                return False
            self.budget.active_retries += 1
            return True

    def end_retry(self) -> None:
        with self.lock:
            self.budget.active_retries -= 1

    def status(self) -> Dict[str, Any]:
        with self.lock:
            self.reap()
            now = self.clock()
            backends = []
            for b in self.backends:
                backends.append({
                    "name": b.name,
                    "address": b.address,
                    "weight": b.weight,
                    "state": b.state(now),
                    "available": b.available(now),
                    "active": b.active,
                    "requests": b.requests,
                    "failures": b.failures,
                    "consecutive_5xx": b.consecutive_5xx,
                    "ejections": b.ejections,
                    "ejected_for": round(max(0.0, b.ejected_until - now), 3),
                    "check": {"passes": b.passes, "fails": b.fails, "last": b.last_check},
                })
            return {
                "algorithm": self.algorithm.name,
                "sticky_cookie": self.sticky_cookie,
                "backends": backends,
                "refused_ejections": self.refused_ejections,
                "retry_budget": {"active_requests": self.budget.active_requests, "active_retries": self.budget.active_retries, "limit": self.budget.limit(), "refused": self.budget.refused},
                "events": list(self.events),
            }


def split_address(address: str) -> Tuple[str, int]:
    host, sep, port = address.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 <= int(port) < 65536:
        raise InputError(f"address {address!r}: expected HOST:PORT, for example 127.0.0.1:9001")
    return host.strip("[]"), int(port)


Forwarder = Callable[[str, str, str, Dict[str, str], Optional[bytes], float], Tuple[str, int, List[Tuple[str, str]], bytes]]


def forward(address: str, method: str, path: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Tuple[str, int, List[Tuple[str, str]], bytes]:
    host, port = split_address(address)
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        try:
            connection.connect()
        except OSError as err:
            return "connect-failure", 503, [], f"upstream connect error: {err}\n".encode()
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return "response", response.status, response.getheaders(), response.read()
        except socket.timeout:
            return "timeout", 504, [], f"upstream request timeout after {timeout:g}s\n".encode()
        except (OSError, http.client.HTTPException) as err:
            return "reset", 502, [], f"upstream reset before a complete response: {err}\n".encode()
    finally:
        connection.close()


def connection_tokens(headers: Dict[str, str]) -> set:
    tokens = set()
    for name, value in headers.items():
        if name.lower() == "connection":
            tokens.update(t.strip().lower() for t in value.split(",") if t.strip())
    return tokens


def upstream_headers(headers: Dict[str, str], client: str) -> Dict[str, str]:
    drop = HOP_BY_HOP | connection_tokens(headers)
    result = {name: value for name, value in headers.items() if name.lower() not in drop and name.lower() != "x-forwarded-for"}
    previous = next((v for k, v in headers.items() if k.lower() == "x-forwarded-for"), "")
    result["X-Forwarded-For"] = f"{previous}, {client}" if previous else client
    return result


def client_headers(headers: Sequence[Tuple[str, str]], method: str) -> List[Tuple[str, str]]:
    drop = HOP_BY_HOP | {"server", "date"} | connection_tokens(dict(headers))
    kept = [(n, v) for n, v in headers if n.lower() not in drop]
    if method == "HEAD":
        kept += [(n, v) for n, v in headers if n.lower() == "content-length"]
    return kept


@dataclass
class ProxyResult:
    status: int
    headers: List[Tuple[str, str]]
    body: bytes
    backend: Optional[str] = None
    attempts: List[str] = field(default_factory=list)
    note: str = ""


def proxy_request(pool: Pool, method: str, path: str, headers: Dict[str, str], body: Optional[bytes], client: str,
                  hash_on: Optional[Tuple[str, str]] = None, timeout: float = 5.0, forwarder: Forwarder = forward) -> ProxyResult:
    lower = {k.lower(): v for k, v in headers.items()}
    key = hash_key(hash_on, lower, path, client)
    sticky = cookie_value(lower.get("cookie"), pool.sticky_cookie) if pool.sticky_cookie else None
    outgoing = upstream_headers(headers, client)
    tried: List[str] = []
    attempts: List[str] = []
    last: Optional[ProxyResult] = None
    retrying = False
    pool.begin_request()
    try:
        while True:
            backend, how = pool.acquire(key, sticky, tried)
            if backend is None:
                if last is not None:
                    last.note = f"no other backend to retry on ({how})"
                    return finish(pool, last, sticky)
                return finish(pool, ProxyResult(503, [("Content-Type", "text/plain")], b"no healthy upstream\n", None, attempts, how), sticky)
            outcome, status, response_headers, payload = forwarder(backend.address, method, path, outgoing, body, timeout)
            pool.release(backend, status)
            if retrying:
                pool.end_retry()
                retrying = False
            attempts.append(f"{backend.name}:{status}")
            tried.append(backend.name)
            last = ProxyResult(status, client_headers(response_headers, method), payload, backend.name, attempts, how)
            reason = retry_reason(method, outcome, status, pool.retry)
            if reason is None:
                if status >= 500 and method.upper() not in IDEMPOTENT_METHODS and outcome != "connect-failure":
                    last.note = f"not retried: {method} is not idempotent"
                return finish(pool, last, sticky)
            if len(attempts) > pool.retry.retries:
                last.note = f"not retried: {pool.retry.retries} retries used"
                return finish(pool, last, sticky)
            if not pool.begin_retry():
                last.note = "not retried: retry budget exhausted"
                return finish(pool, last, sticky)
            retrying = True
    finally:
        if retrying:
            pool.end_retry()
        pool.end_request()


def finish(pool: Pool, result: ProxyResult, sticky: Optional[str]) -> ProxyResult:
    result.headers.append(("x-minilb-backend", result.backend or "-"))
    result.headers.append(("x-minilb-attempts", ",".join(result.attempts) or "-"))
    if result.note:
        result.headers.append(("x-minilb-note", result.note))
    if pool.sticky_cookie and result.backend and result.backend != sticky:
        result.headers.append(("Set-Cookie", f"{pool.sticky_cookie}={result.backend}; Path=/; HttpOnly"))
    return result


def http_probe(backend: Backend, policy: HealthPolicy) -> Tuple[bool, str]:
    host, port = split_address(backend.address)
    connection = http.client.HTTPConnection(host, port, timeout=policy.timeout)
    try:
        connection.request("GET", policy.path, headers={"Host": backend.address, "User-Agent": "minilb-health-check"})
        response = connection.getresponse()
        response.read()
        return 200 <= response.status < 400, f"HTTP {response.status}"
    except socket.timeout:
        return False, f"timeout after {policy.timeout:g}s"
    except (OSError, http.client.HTTPException) as err:
        return False, str(err) or type(err).__name__
    finally:
        connection.close()


class HealthChecker:
    def __init__(self, pool: Pool, prober: Optional[Callable[[Backend, HealthPolicy], Tuple[bool, str]]] = None):
        self.pool = pool
        self.prober = prober or http_probe
        self.stopped = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def run_once(self) -> None:
        if self.pool.health.enabled:
            for backend in self.pool.check_targets():
                ok, detail = self.prober(backend, self.pool.health)
                self.pool.record_check(backend, ok, detail)
        self.pool.reap()

    def loop(self) -> None:
        while not self.stopped.is_set():
            self.run_once()
            self.stopped.wait(self.pool.health.interval)

    def start(self) -> None:
        self.thread = threading.Thread(target=self.loop, name="minilb-health", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=5)


class LoadBalancer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 128

    def __init__(self, address: Tuple[str, int], pool: Pool, hash_on: Optional[Tuple[str, str]] = None, upstream_timeout: float = 5.0):
        super().__init__(address, LoadBalancerHandler)
        self.pool = pool
        self.hash_on = hash_on
        self.upstream_timeout = upstream_timeout


class LoadBalancerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30
    server: LoadBalancer

    def read_body(self) -> Tuple[Optional[bytes], Optional[Tuple[int, str]]]:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            return None, (411, "minilb buffers request bodies and needs Content-Length, not chunked")
        raw = self.headers.get("Content-Length") or "0"
        if not raw.isdigit():
            return None, (400, f"bad Content-Length {raw!r}")
        length = int(raw)
        return (self.rfile.read(length) if length else None), None

    def handle_any(self) -> None:
        body, problem = self.read_body()
        if problem:
            self.close_connection = True
            return self.reply_json(problem[0], {"error": problem[1]})
        if self.path.startswith(ADMIN_PREFIX):
            return self.admin()
        server = self.server
        result = proxy_request(server.pool, self.command, self.path, dict(self.headers.items()), body, self.client_address[0], server.hash_on, server.upstream_timeout)
        self.send_response(result.status)
        for name, value in result.headers:
            self.send_header(name, value)
        if not any(n.lower() == "content-length" for n, _ in result.headers):
            self.send_header("Content-Length", str(len(result.body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(result.body)

    def admin(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(parts.query))
        action = parts.path[len(ADMIN_PREFIX):]
        pool = self.server.pool
        try:
            if action == "status" and self.command == "GET":
                return self.reply_json(200, pool.status())
            if action == "drain" and self.command == "POST":
                backend = pool.drain(required(query, "backend"), float(query.get("timeout", "30")))
                state = "draining" if pool.find(backend.name) else "removed"
                return self.reply_json(200, {"backend": backend.name, "state": state, "in_flight": backend.active})
            if action == "add" and self.command == "POST":
                backend = parse_backend_spec(f"{required(query, 'backend')}={required(query, 'address')},weight={query.get('weight', '1')}")
                pool.add(backend)
                return self.reply_json(201, {"backend": backend.name, "address": backend.address, "weight": backend.weight})
        except UnknownBackend as err:
            return self.reply_json(404, {"error": str(err)})
        except (InputError, ValueError) as err:
            return self.reply_json(400, {"error": str(err)})
        return self.reply_json(404, {"error": f"unknown admin endpoint {self.command} {parts.path}", "endpoints": ["GET /_minilb/status", "POST /_minilb/drain?backend=NAME&timeout=S", "POST /_minilb/add?backend=NAME&address=HOST:PORT&weight=N"]})

    def reply_json(self, status: int, payload: Any) -> None:
        data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = handle_any

    def log_message(self, format: str, *args: Any) -> None:
        return


def required(query: Dict[str, str], name: str) -> str:
    value = query.get(name)
    if not value:
        raise InputError(f"missing query parameter {name}")
    return value


class EchoBackend(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 128

    def __init__(self, address: Tuple[str, int], name: str, delay: float = 0.0, status: int = 200):
        super().__init__(address, EchoBackendHandler)
        self.backend_name = name
        self.delay = delay
        self.status = status
        self.counts = {"requests": 0, "health_checks": 0}
        self.lock = threading.Lock()


class EchoBackendHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30
    server: EchoBackend

    def handle_any(self) -> None:
        server = self.server
        length = self.headers.get("Content-Length") or "0"
        body = self.rfile.read(int(length)) if length.isdigit() and int(length) else b""
        parts = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(parts.query))
        if parts.path == "/_backend/mode":
            with server.lock:
                if "status" in query:
                    server.status = int(query["status"])
                if "delay" in query:
                    server.delay = float(query["delay"])
                state = {"backend": server.backend_name, "status": server.status, "delay": server.delay}
            return self.reply(200, state)
        if parts.path == "/_backend/stats":
            with server.lock:
                state = dict(server.counts, backend=server.backend_name, status=server.status, delay=server.delay)
            return self.reply(200, state)
        if parts.path == "/health":
            with server.lock:
                server.counts["health_checks"] += 1
                status = server.status
            return self.reply(status, {"backend": server.backend_name, "health": status})
        with server.lock:
            server.counts["requests"] += 1
            status, delay = server.status, server.delay
        time.sleep(float(query.get("delay", delay)))
        self.reply(status, {
            "backend": server.backend_name,
            "method": self.command,
            "path": parts.path,
            "status": status,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body_bytes": len(body),
        })

    def reply(self, status: int, payload: Dict[str, Any]) -> None:
        data = (json.dumps(payload, sort_keys=True) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = handle_any

    def log_message(self, format: str, *args: Any) -> None:
        return


@dataclass
class SimBackend:
    name: str
    latency_ms: float
    capacity: int = 4
    weight: int = 1
    error_rate: float = 0.0


def percentile(ordered: Sequence[float], p: float) -> float:
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def simulate(specs: Sequence[SimBackend], algorithm: str, rate: float, requests: int, seed: int = 1, keys: int = 0,
             outlier: Optional[OutlierPolicy] = None, table_size: int = MAGLEV_TABLE_SIZE) -> Dict[str, Any]:
    arrivals_rng = random.Random(seed)
    arrivals: List[Tuple[float, Optional[str]]] = []
    now = 0.0
    for _ in range(requests):
        now += arrivals_rng.expovariate(rate)
        arrivals.append((now, f"user-{arrivals_rng.randrange(keys)}" if keys else None))
    service_rng = random.Random(seed * 7919 + 1)
    clock = [0.0]
    specs_by_name = {s.name: s for s in specs}
    pool = Pool([Backend(s.name, weight=s.weight) for s in specs], make_algorithm(algorithm, random.Random(seed + 2), table_size), outlier=outlier, clock=lambda: clock[0])
    busy = {s.name: 0 for s in specs}
    waiting: Dict[str, collections.deque] = {s.name: collections.deque() for s in specs}
    served: Dict[str, int] = {s.name: 0 for s in specs}
    latencies: List[float] = []
    errors = dropped = 0
    order = itertools.count()
    events: List[Tuple[float, int, str, int, Optional[Backend]]] = [(at, next(order), "arrive", index, None) for index, (at, _) in enumerate(arrivals)]
    heapq.heapify(events)

    def start(index: int, backend: Backend) -> None:
        busy[backend.name] += 1
        service = service_rng.expovariate(1000.0 / specs_by_name[backend.name].latency_ms)
        heapq.heappush(events, (clock[0] + service, next(order), "finish", index, backend))

    while events:
        at, _, kind, index, backend = heapq.heappop(events)
        clock[0] = at
        if kind == "arrive":
            chosen, _ = pool.acquire(arrivals[index][1])
            if chosen is None:
                dropped += 1
            elif busy[chosen.name] < specs_by_name[chosen.name].capacity:
                start(index, chosen)
            else:
                waiting[chosen.name].append(index)
            continue
        assert backend is not None
        busy[backend.name] -= 1
        status = 500 if service_rng.random() < specs_by_name[backend.name].error_rate else 200
        pool.release(backend, status)
        served[backend.name] += 1
        errors += 1 if status >= 500 else 0
        latencies.append((at - arrivals[index][0]) * 1000.0)
        if waiting[backend.name]:
            start(waiting[backend.name].popleft(), backend)
    ordered = sorted(latencies)
    total = max(1, len(latencies))
    return {
        "algorithm": algorithm,
        "requests": requests,
        "served": len(latencies),
        "dropped": dropped,
        "errors": errors,
        "ejections": sum(b.ejections for b in pool.backends),
        "ejections_refused": pool.refused_ejections,
        "latency_ms": {
            "p50": round(percentile(ordered, 50), 1),
            "p95": round(percentile(ordered, 95), 1),
            "p99": round(percentile(ordered, 99), 1),
            "max": round(ordered[-1] if ordered else 0.0, 1),
            "mean": round(sum(ordered) / total, 1),
        },
        "share": {name: round(count / total, 4) for name, count in served.items()},
    }


def remap_report(names: Sequence[Tuple[str, int]], removed: Sequence[str], added: Sequence[Tuple[str, int]], keys: int, table_size: int = MAGLEV_TABLE_SIZE) -> Dict[str, Any]:
    known = {n for n, _ in names}
    for name in removed:
        if name not in known:
            raise InputError(f"--remove {name}: not one of {', '.join(sorted(known))}")
    before = [Backend(n, weight=w) for n, w in names]
    after = [b for b in before if b.name not in removed] + [Backend(n, weight=w) for n, w in added]
    if not after:
        raise InputError("removing every backend leaves nothing to hash onto")
    rows = []
    for algorithm in (ModuloHash(), RingHash(), Maglev(table_size)):
        moved = moved_from_kept = 0
        owners: Dict[str, int] = {b.name: 0 for b in after}
        for index in range(keys):
            key = f"key-{index}"
            old = algorithm.pick(before, key).name
            new = algorithm.pick(after, key).name
            owners[new] += 1
            if old != new:
                moved += 1
                if old not in removed:
                    moved_from_kept += 1
        rows.append({
            "algorithm": algorithm.name,
            "moved": round(moved / keys, 4),
            "moved_from_kept": round(moved_from_kept / keys, 4),
            "share": {name: round(count / keys, 4) for name, count in owners.items()},
        })
    return {"before": [n for n, _ in names], "after": [b.name for b in after], "keys": keys, "results": rows}


def parse_backend_spec(text: str) -> Backend:
    name, sep, rest = text.partition("=")
    if not sep or not name.strip() or not rest:
        raise InputError(f"--backend {text!r}: expected NAME=HOST:PORT[,weight=N]")
    address, *options = rest.split(",")
    split_address(address)
    backend = Backend(name.strip(), address=address)
    for option in options:
        key, sep, value = option.partition("=")
        if key != "weight" or not value.isdigit() or int(value) < 1:
            raise InputError(f"--backend {text!r}: unknown option {option!r} (only weight=N with N >= 1)")
        backend.weight = int(value)
    return backend


def parse_sim_spec(text: str) -> SimBackend:
    name, sep, rest = text.partition("=")
    if not sep or not name.strip() or not rest:
        raise InputError(f"--backend {text!r}: expected NAME=LATENCY_MS[,capacity=N][,weight=N][,errors=RATE]")
    latency, *options = rest.split(",")
    try:
        spec = SimBackend(name.strip(), float(latency))
        for option in options:
            key, _, value = option.partition("=")
            if key == "capacity":
                spec.capacity = int(value)
            elif key == "weight":
                spec.weight = int(value)
            elif key == "errors":
                spec.error_rate = float(value)
            else:
                raise InputError(f"--backend {text!r}: unknown option {key!r} (capacity, weight, errors)")
    except ValueError:
        raise InputError(f"--backend {text!r}: numbers expected, for example slow=100,capacity=4,errors=0.1") from None
    if spec.latency_ms <= 0 or spec.capacity < 1 or spec.weight < 1 or not 0.0 <= spec.error_rate <= 1.0:
        raise InputError(f"--backend {text!r}: latency > 0, capacity >= 1, weight >= 1, 0 <= errors <= 1")
    return spec


def parse_names(text: str) -> List[Tuple[str, int]]:
    pairs = []
    for item in [i.strip() for i in text.split(",") if i.strip()]:
        name, sep, weight = item.partition(":")
        if sep and (not weight.isdigit() or int(weight) < 1):
            raise InputError(f"backend {item!r}: expected NAME or NAME:WEIGHT with WEIGHT >= 1")
        pairs.append((name, int(weight) if sep else 1))
    if not pairs:
        raise InputError("no backends given: pass a,b,c or a:5,b:1")
    if len({n for n, _ in pairs}) != len(pairs):
        raise InputError(f"duplicate backend names in {text!r}")
    return pairs


def parse_list(text: str) -> List[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def parse_active(text: str) -> Dict[str, int]:
    active = {}
    for item in parse_list(text):
        name, sep, value = item.partition("=")
        if not sep or not value.isdigit():
            raise InputError(f"--active {item!r}: expected NAME=COUNT")
        active[name] = int(value)
    return active


def pick_report(algorithm: str, names: Sequence[Tuple[str, int]], removed: Sequence[str], active: Dict[str, int], count: int, keys: Sequence[str], seed: int, table_size: int) -> Dict[str, Any]:
    backends = [Backend(n, weight=w, active=active.get(n, 0)) for n, w in names if n not in removed]
    if not backends:
        raise InputError("no backends left after --remove")
    chooser = make_algorithm(algorithm, random.Random(seed), table_size)
    if keys:
        return {"algorithm": algorithm, "backends": [b.name for b in backends], "keys": {key: chooser.pick(backends, key).name for key in keys}}
    sequence = [chooser.pick(backends).name for _ in range(count)]
    return {"algorithm": algorithm, "backends": [b.name for b in backends], "sequence": sequence, "counts": dict(collections.Counter(sequence))}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minilb", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the load balancer in front of real HTTP backends")
    serve.add_argument("--listen", default="127.0.0.1:8080", help="HOST:PORT (port 0 picks a free one)")
    serve.add_argument("--backend", action="append", default=[], metavar="NAME=HOST:PORT[,weight=N]")
    serve.add_argument("--algorithm", default="round_robin", choices=ALGORITHMS)
    serve.add_argument("--hash-on", help="hash key for ring_hash, maglev, modulo_hash: header:NAME, cookie:NAME, path or source")
    serve.add_argument("--table-size", type=int, default=MAGLEV_TABLE_SIZE, help="Maglev lookup table size, a prime")
    serve.add_argument("--seed", type=int, help="seed for p2c's random choices")
    serve.add_argument("--check-path", default="/health")
    serve.add_argument("--check-interval", type=float, default=2.0, help="seconds between active checks (HAProxy inter)")
    serve.add_argument("--check-timeout", type=float, default=1.0)
    serve.add_argument("--rise", type=int, default=2, help="consecutive passed checks before a down backend is up")
    serve.add_argument("--fall", type=int, default=3, help="consecutive failed checks before an up backend is down")
    serve.add_argument("--no-check", action="store_true", help="disable active health checks")
    serve.add_argument("--consecutive-5xx", type=int, default=5, help="passive: eject after this many 5xx in a row (0 disables)")
    serve.add_argument("--base-ejection", type=float, default=30.0, help="seconds; multiplied by the number of ejections")
    serve.add_argument("--max-ejection", type=float, default=300.0)
    serve.add_argument("--max-ejection-percent", type=int, default=10, help="an ejection may not push the ejected share above this")
    serve.add_argument("--always-eject-one-host", action="store_true", help="allow one ejection even when the percentage forbids it")
    serve.add_argument("--retries", type=int, default=1, help="extra attempts per request")
    serve.add_argument("--retry-on", default="connect-failure,gateway-error", help=f"comma list of {', '.join(RETRY_CONDITIONS)}")
    serve.add_argument("--budget-percent", type=float, default=20.0, help="concurrent retries as a percentage of active requests")
    serve.add_argument("--min-retry-concurrency", type=int, default=3)
    serve.add_argument("--sticky-cookie", help="cookie name for sticky sessions, for example MINILB")
    serve.add_argument("--upstream-timeout", type=float, default=5.0)
    serve.add_argument("--print-config", action="store_true", help="print the effective configuration as JSON and exit")
    serve.add_argument("--json", action="store_true", help="events as JSON lines on stderr")
    serve.add_argument("--quiet", action="store_true", help="no event lines")

    pick = sub.add_parser("pick", help="ask one algorithm for its choices, without traffic")
    pick.add_argument("--algorithm", required=True, choices=ALGORITHMS)
    pick.add_argument("--backends", required=True, help="a,b,c or a:5,b:1,c:1 (NAME:WEIGHT)")
    pick.add_argument("--remove", default="", help="names to take out first, comma-separated")
    pick.add_argument("--active", default="", help="requests in flight per backend, for least_conn and p2c: a=3,b=0")
    pick.add_argument("--count", type=int, default=10)
    pick.add_argument("--key", action="append", default=[], help="hash key, repeatable")
    pick.add_argument("--table-size", type=int, default=MAGLEV_TABLE_SIZE)
    pick.add_argument("--seed", type=int, default=1)
    pick.add_argument("--json", action="store_true")

    remap = sub.add_parser("remap", help="how many keys move when backends are removed or added, per hashing scheme")
    remap.add_argument("--backends", required=True)
    remap.add_argument("--remove", default="")
    remap.add_argument("--add", default="")
    remap.add_argument("--keys", type=int, default=10000)
    remap.add_argument("--table-size", type=int, default=MAGLEV_TABLE_SIZE)
    remap.add_argument("--json", action="store_true")

    sim = sub.add_parser("simulate", help="replay Poisson traffic against backends of different speeds")
    sim.add_argument("--backend", action="append", default=[], metavar="NAME=LATENCY_MS[,capacity=N][,weight=N][,errors=RATE]")
    sim.add_argument("--algorithms", default="round_robin,least_conn,p2c")
    sim.add_argument("--rate", type=float, default=150.0, help="requests per second")
    sim.add_argument("--requests", type=int, default=5000)
    sim.add_argument("--keys", type=int, default=0, help="distinct hash keys (users) for the hash algorithms")
    sim.add_argument("--consecutive-5xx", type=int, default=0)
    sim.add_argument("--base-ejection", type=float, default=30.0)
    sim.add_argument("--max-ejection-percent", type=int, default=10)
    sim.add_argument("--always-eject-one-host", action="store_true")
    sim.add_argument("--seed", type=int, default=1)
    sim.add_argument("--table-size", type=int, default=MAGLEV_TABLE_SIZE)
    sim.add_argument("--json", action="store_true")

    backend = sub.add_parser("backend", help="a test backend: echoes requests as JSON, answers /health, switches with /_backend/mode")
    backend.add_argument("--name", required=True)
    backend.add_argument("--host", default="127.0.0.1")
    backend.add_argument("--port", type=int, default=9001)
    backend.add_argument("--delay", type=float, default=0.0, help="seconds before each non-health response")
    backend.add_argument("--status", type=int, default=200)
    return parser


def pool_from_args(args: argparse.Namespace) -> Tuple[Pool, Optional[Tuple[str, str]], Dict[str, Any]]:
    if not args.backend:
        raise InputError("serve needs at least one --backend NAME=HOST:PORT")
    backends = [parse_backend_spec(spec) for spec in args.backend]
    if len({b.name for b in backends}) != len(backends):
        raise InputError("backend names must be unique")
    retry_on = tuple(parse_list(args.retry_on))
    for condition in retry_on:
        if condition not in RETRY_CONDITIONS:
            raise InputError(f"--retry-on {condition!r}: choose from {', '.join(RETRY_CONDITIONS)}")
    if args.rise < 1 or args.fall < 1:
        raise InputError("--rise and --fall must be at least 1")
    if args.retries < 0 or not 0 <= args.budget_percent <= 100 or not 0 <= args.max_ejection_percent <= 100:
        raise InputError("--retries >= 0, --budget-percent and --max-ejection-percent between 0 and 100")
    split_address(args.listen)
    hash_on = parse_hash_on(args.hash_on) if args.hash_on else None
    health = HealthPolicy(args.check_path, args.check_interval, args.check_timeout, args.rise, args.fall, not args.no_check)
    outlier = OutlierPolicy(args.consecutive_5xx, args.base_ejection, args.max_ejection, args.max_ejection_percent, args.always_eject_one_host)
    retry = RetryPolicy(args.retries, retry_on, args.budget_percent, args.min_retry_concurrency)
    algorithm = make_algorithm(args.algorithm, random.Random(args.seed), args.table_size)
    pool = Pool(backends, algorithm, health, outlier, retry, args.sticky_cookie)
    warnings = []
    if args.algorithm in HASH_ALGORITHMS and hash_on is None:
        warnings.append(f"{args.algorithm} without --hash-on has no key and falls back to round robin")
    if outlier.consecutive_5xx and not outlier.always_eject_one_host and 100 > len(backends) * outlier.max_ejection_percent:
        warnings.append(f"max_ejection_percent {outlier.max_ejection_percent} with {len(backends)} backends forbids every ejection; raise it to {math.ceil(100 / len(backends))} or pass --always-eject-one-host")
    config = {
        "listen": args.listen,
        "algorithm": args.algorithm,
        "hash_on": ":".join(hash_on) if hash_on else None,
        "backends": [{"name": b.name, "address": b.address, "weight": b.weight} for b in backends],
        "health": asdict(health),
        "outlier": asdict(outlier),
        "retry": dict(asdict(retry), retry_on=list(retry_on)),
        "sticky_cookie": args.sticky_cookie,
        "upstream_timeout": args.upstream_timeout,
        "warnings": warnings,
    }
    return pool, hash_on, config


def event_printer(as_json: bool) -> Callable[[Dict[str, Any]], None]:
    def emit(entry: Dict[str, Any]) -> None:
        line = json.dumps(entry, sort_keys=True) if as_json else f"[minilb] t={entry['t']:.3f} {entry['backend']} {entry['event']}: {entry['reason']}"
        print(line, file=sys.stderr, flush=True)
    return emit


def render_simulation(results: Sequence[Dict[str, Any]], specs: Sequence[SimBackend], rate: float, requests: int, seed: int) -> str:
    names = [s.name for s in specs]
    lines = [f"{len(specs)} backends ({', '.join(f'{s.name} {s.latency_ms:g}ms x{s.capacity}' for s in specs)}), {rate:g} req/s, {requests} requests, seed {seed}"]
    lines.append(f"{'algorithm':<22}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}{'errors':>8}  share {'/'.join(names)}")
    for r in results:
        lat = r["latency_ms"]
        share = "/".join(f"{r['share'][n] * 100:.0f}%" for n in names)
        lines.append(f"{r['algorithm']:<22}{show_ms(lat['p50']):>9}{show_ms(lat['p95']):>9}{show_ms(lat['p99']):>9}{show_ms(lat['max']):>9}{r['errors']:>8}  {share}")
    return "\n".join(lines)


def show_ms(value: float) -> str:
    return f"{value / 1000:.2f}s" if value >= 1000 else f"{value:.0f}ms"


def render_remap(report: Dict[str, Any]) -> str:
    lines = [f"{', '.join(report['before'])} -> {', '.join(report['after'])}, {report['keys']} keys"]
    lines.append(f"{'scheme':<14}{'moved':>8}{'moved from a backend that stayed':>35}  share after")
    for row in report["results"]:
        share = " ".join(f"{n}={v * 100:.1f}%" for n, v in row["share"].items())
        lines.append(f"{row['algorithm']:<14}{row['moved'] * 100:>7.1f}%{row['moved_from_kept'] * 100:>34.1f}%  {share}")
    return "\n".join(lines)


def run_serve(args: argparse.Namespace) -> int:
    pool, hash_on, config = pool_from_args(args)
    if args.print_config:
        print(json.dumps(config, indent=2, sort_keys=True))
        return 0
    for warning in config["warnings"]:
        print(f"minilb: warning: {warning}", file=sys.stderr, flush=True)
    if not args.quiet:
        pool.listeners.append(event_printer(args.json))
    server = LoadBalancer(split_address(args.listen), pool, hash_on, args.upstream_timeout)
    checker = HealthChecker(pool)
    checker.start()
    host = server.server_address[0]
    print(f"minilb listening on http://{host}:{server.server_address[1]} algorithm={args.algorithm} backends={','.join(b.name for b in pool.backends)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        checker.stop()
        server.server_close()
    return 0


def run_backend(args: argparse.Namespace) -> int:
    server = EchoBackend((args.host, args.port), args.name, args.delay, args.status)
    print(f"backend {args.name} listening on http://{args.host}:{server.server_address[1]} status={args.status} delay={args.delay:g}s", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "serve":
            return run_serve(args)
        if args.command == "backend":
            return run_backend(args)
        if args.command == "pick":
            report = pick_report(args.algorithm, parse_names(args.backends), parse_list(args.remove), parse_active(args.active), args.count, args.key, args.seed, args.table_size)
            if args.json:
                print(json.dumps(report, indent=2))
            elif "keys" in report:
                for key, owner in report["keys"].items():
                    print(f"{key} -> {owner}")
            else:
                print(" ".join(report["sequence"]))
                print("counts: " + ", ".join(f"{n}={c}" for n, c in sorted(report["counts"].items())))
            return 0
        if args.command == "remap":
            if args.keys < 1:
                raise InputError("--keys must be at least 1")
            report = remap_report(parse_names(args.backends), parse_list(args.remove), parse_names(args.add) if args.add else [], args.keys, args.table_size)
            print(json.dumps(report, indent=2) if args.json else render_remap(report))
            return 0
        specs = [parse_sim_spec(s) for s in (args.backend or DEFAULT_SIM_BACKENDS)]
        if len({s.name for s in specs}) != len(specs):
            raise InputError("backend names must be unique")
        if args.rate <= 0 or args.requests < 1:
            raise InputError("--rate must be > 0 and --requests >= 1")
        outlier = OutlierPolicy(args.consecutive_5xx, args.base_ejection, max_ejection_percent=args.max_ejection_percent, always_eject_one_host=args.always_eject_one_host) if args.consecutive_5xx else None
        results = [simulate(specs, name, args.rate, args.requests, args.seed, args.keys, outlier, args.table_size) for name in parse_list(args.algorithms)]
        print(json.dumps(results, indent=2) if args.json else render_simulation(results, specs, args.rate, args.requests, args.seed))
        return 0
    except InputError as exc:
        print(f"minilb: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"minilb: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
