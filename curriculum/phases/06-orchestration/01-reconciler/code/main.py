from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

HASH_LABEL = "pod-template-hash"
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
READY_AFTER_ANNOTATION = "minictl/ready-after-ticks"
SAFE_ALPHABET = "bcdfghjklmnpqrstvwxz2456789"
STATUS_KINDS = ("Pod", "ReplicaSet", "Deployment")
WORKLOAD_KINDS = ("ReplicaSet", "Deployment")
IMMUTABLE_METADATA = ("uid", "creationTimestamp", "generation", "namespace", "name")
CONFLICT_TEXT = "the object has been modified; please apply your changes to the latest version and try again"

Obj = Dict[str, Any]


class ApiError(Exception):
    code = 500
    reason = "InternalError"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(ApiError):
    code = 404
    reason = "NotFound"


class AlreadyExists(ApiError):
    code = 409
    reason = "AlreadyExists"


class Conflict(ApiError):
    code = 409
    reason = "Conflict"


class Invalid(ApiError):
    code = 422
    reason = "Invalid"


class Gone(ApiError):
    code = 410
    reason = "Expired"


class Stalled(RuntimeError):
    pass


def labels_of(obj: Obj) -> Dict[str, str]:
    return (obj.get("metadata") or {}).get("labels") or {}


def key_of(obj: Obj) -> str:
    meta = obj["metadata"]
    return f"{meta.get('namespace', 'default')}/{meta['name']}"


def split_key(key: str) -> Tuple[str, str]:
    namespace, name = key.split("/", 1)
    return namespace, name


def ref_of(obj: Obj) -> str:
    return f"{obj['kind']}/{key_of(obj)}"


def int_of(value: Any, default: int = 0) -> int:
    return default if value is None else int(value)


def owner_reference(owner: Obj) -> Obj:
    return {
        "apiVersion": owner.get("apiVersion", "v1"),
        "kind": owner["kind"],
        "name": owner["metadata"]["name"],
        "uid": owner["metadata"]["uid"],
        "controller": True,
        "blockOwnerDeletion": True,
    }


def controller_of(obj: Obj) -> Optional[Obj]:
    for ref in (obj.get("metadata") or {}).get("ownerReferences") or []:
        if ref.get("controller"):
            return ref
    return None


@dataclass(frozen=True)
class Requirement:
    key: str
    operator: str
    values: Tuple[str, ...] = ()


TERM_SPLIT = re.compile(r",(?![^()]*\))")
SET_TERM = re.compile(r"^\s*([A-Za-z0-9_./-]+)\s+(in|notin)\s*\(([^)]*)\)\s*$")
EQUALITY_TERM = re.compile(r"^\s*([A-Za-z0-9_./-]+)\s*(==|=|!=)\s*([A-Za-z0-9_.-]*)\s*$")
EXISTS_TERM = re.compile(r"^\s*(!?)([A-Za-z0-9_./-]+)\s*$")


def parse_selector(text: str) -> List[Requirement]:
    requirements: List[Requirement] = []
    if not text.strip():
        return requirements
    for term in TERM_SPLIT.split(text):
        match = SET_TERM.match(term)
        if match:
            values = tuple(v.strip() for v in match.group(3).split(",") if v.strip())
            requirements.append(Requirement(match.group(1), match.group(2), values))
            continue
        match = EQUALITY_TERM.match(term)
        if match:
            operator = "notin" if match.group(2) == "!=" else "in"
            requirements.append(Requirement(match.group(1), operator, (match.group(3),)))
            continue
        match = EXISTS_TERM.match(term)
        if match:
            requirements.append(Requirement(match.group(2), "!exists" if match.group(1) else "exists"))
            continue
        raise Invalid(f"unable to parse requirement {term.strip()!r} in selector {text!r}")
    return requirements


def selector_from_spec(spec: Optional[Obj]) -> List[Requirement]:
    spec = spec or {}
    requirements = [Requirement(k, "in", (str(v),)) for k, v in sorted((spec.get("matchLabels") or {}).items())]
    operators = {"In": "in", "NotIn": "notin", "Exists": "exists", "DoesNotExist": "!exists"}
    for expression in spec.get("matchExpressions") or []:
        operator = operators.get(expression.get("operator"))
        if operator is None:
            raise Invalid(f"unknown selector operator {expression.get('operator')!r}")
        values = tuple(str(v) for v in expression.get("values") or [])
        requirements.append(Requirement(expression["key"], operator, values))
    return requirements


def selector_matches(requirements: Sequence[Requirement], labels: Dict[str, str]) -> bool:
    for req in requirements:
        present = req.key in labels
        if req.operator == "in" and not (present and labels[req.key] in req.values):
            return False
        if req.operator == "notin" and present and labels[req.key] in req.values:
            return False
        if req.operator == "exists" and not present:
            return False
        if req.operator == "!exists" and present:
            return False
    return True


def fnv1a_32(data: bytes) -> int:
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def safe_encode(text: str) -> str:
    return "".join(SAFE_ALPHABET[ord(char) % len(SAFE_ALPHABET)] for char in text)


def pod_template_hash(template: Obj) -> str:
    stripped = copy.deepcopy(template)
    (stripped.get("metadata") or {}).get("labels", {}).pop(HASH_LABEL, None)
    canonical = json.dumps(stripped, sort_keys=True, separators=(",", ":"))
    return safe_encode(str(fnv1a_32(canonical.encode("utf-8"))))


def generated_suffix(uid: str) -> str:
    digest = hashlib.sha256(uid.encode("utf-8")).digest()
    return "".join(SAFE_ALPHABET[byte % len(SAFE_ALPHABET)] for byte in digest[:5])


def validate(obj: Obj) -> None:
    kind = obj.get("kind")
    if kind not in WORKLOAD_KINDS:
        return
    name = obj["metadata"]["name"]
    spec = obj.get("spec") or {}
    if not selector_from_spec(spec.get("selector")):
        raise Invalid(f'{kind} "{name}" is invalid: spec.selector: Required value')
    template_labels = ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
    if not selector_matches(selector_from_spec(spec.get("selector")), template_labels):
        raise Invalid(f'{kind} "{name}" is invalid: spec.template.metadata.labels: `selector` does not match template `labels`')
    if int_of(spec.get("replicas"), 1) < 0:
        raise Invalid(f'{kind} "{name}" is invalid: spec.replicas: must be greater than or equal to 0')
    strategy = (spec.get("strategy") or {}).get("type", "RollingUpdate")
    if kind == "Deployment" and strategy != "RollingUpdate":
        raise Invalid(f'{kind} "{name}" is invalid: spec.strategy.type: Unsupported value: "{strategy}": supported values: "RollingUpdate"')


def comparable(obj: Obj) -> Obj:
    stripped = copy.deepcopy(obj)
    stripped["metadata"].pop("resourceVersion", None)
    return stripped


def watch_event(entry: Obj, namespace: Optional[str], selector: Sequence[Requirement]) -> Optional[Obj]:
    obj, previous = entry["object"], entry["previous"]
    if namespace is not None and obj["metadata"].get("namespace") != namespace:
        return None
    matches_now = selector_matches(selector, labels_of(obj))
    matched_before = previous is not None and selector_matches(selector, labels_of(previous))
    if entry["type"] == "DELETED":
        etype = "DELETED" if matched_before else None
    elif entry["type"] == "ADDED":
        etype = "ADDED" if matches_now else None
    elif matches_now and matched_before:
        etype = "MODIFIED"
    elif matches_now:
        etype = "ADDED"
    else:
        etype = "DELETED" if matched_before else None
    return {"type": etype, "object": copy.deepcopy(obj)} if etype else None


class ApiServer:
    def __init__(self, clock: Optional[Callable[[], int]] = None):
        self.clock = clock or (lambda: 0)
        self.objects: Dict[Tuple[str, str, str], Obj] = {}
        self.revision = 0
        self.uids = 0
        self.history: List[Obj] = []
        self.compacted = 0
        self.hooks: List[Callable[[str, str, str, str], None]] = []

    def bump(self) -> str:
        self.revision += 1
        return str(self.revision)

    def record(self, etype: str, obj: Obj, previous: Optional[Obj]) -> None:
        self.history.append({"rv": self.revision, "type": etype, "object": copy.deepcopy(obj), "previous": copy.deepcopy(previous)})

    def create(self, obj: Obj) -> Obj:
        new = copy.deepcopy(obj)
        kind = new.get("kind")
        if not kind:
            raise Invalid("kind is required")
        meta = new.setdefault("metadata", {})
        meta.setdefault("namespace", "default")
        self.uids += 1
        meta["uid"] = "uid-%04d" % self.uids
        if not meta.get("name"):
            if not meta.get("generateName"):
                raise Invalid("metadata.name or metadata.generateName is required")
            meta["name"] = meta["generateName"] + generated_suffix(meta["uid"])
        key = (kind, meta["namespace"], meta["name"])
        if key in self.objects:
            raise AlreadyExists(f'{kind.lower()}s "{meta["name"]}" already exists')
        validate(new)
        meta["generation"] = 1
        meta["creationTimestamp"] = self.clock()
        if kind in STATUS_KINDS:
            new["status"] = {}
        meta["resourceVersion"] = self.bump()
        self.objects[key] = new
        self.record("ADDED", new, None)
        return copy.deepcopy(new)

    def get(self, kind: str, namespace: str, name: str) -> Obj:
        try:
            return copy.deepcopy(self.objects[(kind, namespace, name)])
        except KeyError:
            raise NotFound(f'{kind.lower()}s "{name}" not found') from None

    def list(self, kind: str, namespace: Optional[str] = None, selector: Union[str, Sequence[Requirement], None] = None) -> Tuple[List[Obj], str]:
        requirements = parse_selector(selector) if isinstance(selector, str) else list(selector or [])
        items = []
        for key in sorted(self.objects):
            obj = self.objects[key]
            if key[0] == kind and (namespace is None or key[1] == namespace) and selector_matches(requirements, labels_of(obj)):
                items.append(copy.deepcopy(obj))
        return items, str(self.revision)

    def write(self, obj: Obj, subresource: str) -> Obj:
        meta = obj.get("metadata") or {}
        kind, namespace, name = obj.get("kind", ""), meta.get("namespace", "default"), meta.get("name", "")
        for hook in list(self.hooks):
            hook(subresource, kind, namespace, name)
        key = (kind, namespace, name)
        current = self.objects.get(key)
        if current is None:
            raise NotFound(f'{kind.lower()}s "{name}" not found')
        sent = meta.get("resourceVersion")
        if sent not in (None, "") and sent != current["metadata"]["resourceVersion"]:
            raise Conflict(f'Operation cannot be fulfilled on {kind.lower()}s "{name}": {CONFLICT_TEXT}')
        if subresource == "status":
            candidate = copy.deepcopy(current)
            candidate["status"] = copy.deepcopy(obj.get("status") or {})
        else:
            candidate = copy.deepcopy(obj)
            if kind in STATUS_KINDS:
                candidate["status"] = copy.deepcopy(current.get("status") or {})
            for field in IMMUTABLE_METADATA:
                if field in current["metadata"]:
                    candidate["metadata"][field] = current["metadata"][field]
        if comparable(candidate) == comparable(current):
            return copy.deepcopy(current)
        validate(candidate)
        if subresource != "status" and candidate.get("spec") != current.get("spec"):
            candidate["metadata"]["generation"] = current["metadata"]["generation"] + 1
        candidate["metadata"]["resourceVersion"] = self.bump()
        self.objects[key] = candidate
        self.record("MODIFIED", candidate, current)
        return copy.deepcopy(candidate)

    def update(self, obj: Obj) -> Obj:
        return self.write(obj, "update")

    def update_status(self, obj: Obj) -> Obj:
        return self.write(obj, "status")

    def delete(self, kind: str, namespace: str, name: str, orphan: bool = False) -> Obj:
        current = self.objects.pop((kind, namespace, name), None)
        if current is None:
            raise NotFound(f'{kind.lower()}s "{name}" not found')
        gone = copy.deepcopy(current)
        gone["metadata"]["resourceVersion"] = self.bump()
        self.record("DELETED", gone, current)
        if orphan:
            self.release_dependents(current["metadata"]["uid"])
        return gone

    def release_dependents(self, uid: str) -> None:
        for key in sorted(self.objects):
            obj = self.objects[key]
            refs = obj["metadata"].get("ownerReferences") or []
            if any(ref["uid"] == uid for ref in refs):
                updated = copy.deepcopy(obj)
                updated["metadata"]["ownerReferences"] = [ref for ref in refs if ref["uid"] != uid]
                self.write(updated, "update")

    def garbage_collect(self) -> List[Tuple[str, str, str]]:
        removed: List[Tuple[str, str, str]] = []
        while True:
            live = {obj["metadata"]["uid"] for obj in self.objects.values()}
            doomed = [
                key for key in sorted(self.objects)
                if self.objects[key]["metadata"].get("ownerReferences")
                and not any(ref["uid"] in live for ref in self.objects[key]["metadata"]["ownerReferences"])
            ]
            if not doomed:
                return removed
            for key in doomed:
                self.delete(*key)
                removed.append(key)

    def compact(self, keep: int = 0) -> None:
        cut = self.revision - keep
        self.history = [entry for entry in self.history if entry["rv"] > cut]
        self.compacted = max(self.compacted, cut)

    def events_since(self, kind: str, revision: int, namespace: Optional[str] = None, selector: Union[str, Sequence[Requirement], None] = None) -> Tuple[List[Obj], int]:
        if revision < self.compacted:
            raise Gone(f"too old resource version: {revision} ({self.compacted})")
        requirements = parse_selector(selector) if isinstance(selector, str) else list(selector or [])
        events = []
        for entry in self.history:
            if entry["rv"] > revision and entry["object"]["kind"] == kind:
                event = watch_event(entry, namespace, requirements)
                if event:
                    events.append(event)
        return events, self.revision


class WorkQueue:
    def __init__(self, base_delay: int = 1, max_delay: int = 32):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.due: Dict[str, int] = {}
        self.processing: set = set()
        self.failures: Dict[str, int] = {}

    def add(self, key: str, now: int) -> None:
        self.add_after(key, 0, now)

    def add_after(self, key: str, delay: int, now: int) -> None:
        when = now + delay
        if key not in self.due or when < self.due[key]:
            self.due[key] = when

    def add_rate_limited(self, key: str, now: int) -> int:
        failures = self.failures.get(key, 0)
        delay = min(self.base_delay * 2 ** failures, self.max_delay)
        self.failures[key] = failures + 1
        self.add_after(key, delay, now)
        return delay

    def forget(self, key: str) -> None:
        self.failures.pop(key, None)

    def requeues(self, key: str) -> int:
        return self.failures.get(key, 0)

    def ready(self, now: int) -> List[str]:
        return [key for key, due in self.due.items() if due <= now and key not in self.processing]

    def get(self, now: int) -> Optional[str]:
        ready = self.ready(now)
        if not ready:
            return None
        key = min(ready, key=lambda k: self.due[k])
        del self.due[key]
        self.processing.add(key)
        return key

    def done(self, key: str) -> None:
        self.processing.discard(key)

    def __len__(self) -> int:
        return len(self.due)


class Informer:
    def __init__(self, api: ApiServer, kind: str, handler: Callable[[str, Obj], None], resync_period: int = 0):
        self.api = api
        self.kind = kind
        self.handler = handler
        self.resync_period = resync_period
        self.cache: Dict[str, Obj] = {}
        self.revision: Optional[int] = None
        self.last_resync = 0
        self.lists = 0
        self.drop = False
        self.last_gone = ""

    def emit(self, etype: str, obj: Obj) -> None:
        if not self.drop:
            self.handler(etype, obj)

    def poll(self, now: int) -> None:
        if self.revision is None:
            self.relist()
        else:
            try:
                events, revision = self.api.events_since(self.kind, self.revision)
            except Gone as err:
                self.last_gone = err.message
                self.relist()
            else:
                for event in events:
                    obj = event["object"]
                    if event["type"] == "DELETED":
                        self.cache.pop(key_of(obj), None)
                    else:
                        self.cache[key_of(obj)] = obj
                    self.emit(event["type"], obj)
                self.revision = revision
        if self.resync_period and now - self.last_resync >= self.resync_period:
            self.last_resync = now
            for obj in list(self.cache.values()):
                self.emit("SYNC", obj)

    def relist(self) -> None:
        items, revision = self.api.list(self.kind)
        fresh = {key_of(obj): obj for obj in items}
        for key, obj in list(self.cache.items()):
            if key not in fresh:
                self.emit("DELETED", obj)
        for key, obj in fresh.items():
            known = self.cache.get(key)
            if known is None:
                self.emit("ADDED", obj)
            elif known["metadata"]["resourceVersion"] != obj["metadata"]["resourceVersion"]:
                self.emit("MODIFIED", obj)
        self.cache = fresh
        self.revision = int(revision)
        self.lists += 1


def claim(api: ApiServer, owner: Obj, candidates: List[Obj], selector: Sequence[Requirement]) -> List[Obj]:
    claimed = []
    for obj in candidates:
        ref = controller_of(obj)
        matches = selector_matches(selector, labels_of(obj))
        if ref is None and matches:
            adopted = copy.deepcopy(obj)
            adopted["metadata"].setdefault("ownerReferences", []).append(owner_reference(owner))
            claimed.append(api.update(adopted))
        elif ref is not None and ref["uid"] == owner["metadata"]["uid"]:
            if matches:
                claimed.append(obj)
            else:
                released = copy.deepcopy(obj)
                released["metadata"]["ownerReferences"] = [r for r in released["metadata"]["ownerReferences"] if r["uid"] != ref["uid"]]
                api.update(released)
    return claimed


def pod_phase(pod: Obj) -> str:
    return (pod.get("status") or {}).get("phase", "Pending")


def pod_active(pod: Obj) -> bool:
    return pod_phase(pod) not in ("Succeeded", "Failed")


def pod_ready(pod: Obj) -> bool:
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in (pod.get("status") or {}).get("conditions") or [])


def pod_status(phase: str, ready: bool) -> Obj:
    return {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}


def deletion_rank(pod: Obj) -> Tuple[int, int, str]:
    rank = 0 if pod_phase(pod) == "Pending" else (2 if pod_ready(pod) else 1)
    return rank, -int_of(pod["metadata"].get("creationTimestamp")), pod["metadata"]["name"]


def pod_from_template(rs: Obj) -> Obj:
    template = copy.deepcopy(rs["spec"]["template"])
    meta = template.setdefault("metadata", {})
    meta["generateName"] = rs["metadata"]["name"] + "-"
    meta["namespace"] = rs["metadata"]["namespace"]
    meta["ownerReferences"] = [owner_reference(rs)]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": meta, "spec": template["spec"]}


class Controller:
    name = "controller"
    kind = ""
    owned_kinds: Tuple[str, ...] = ()

    def __init__(self, cluster: "Cluster", resync_period: int = 0):
        self.cluster = cluster
        self.api = cluster.api
        self.queue = WorkQueue(cluster.backoff_base, cluster.backoff_cap)
        self.informers: Dict[str, Informer] = {self.kind: Informer(self.api, self.kind, self.on_primary, resync_period)}
        for kind in self.owned_kinds:
            self.informers[kind] = Informer(self.api, kind, self.on_owned)
        self.paused = False
        self.stats = {"events": 0, "reconciles": 0, "conflicts": 0, "errors": 0}

    def on_primary(self, etype: str, obj: Obj) -> None:
        self.stats["events"] += 1
        self.queue.add(key_of(obj), self.cluster.now)

    def on_owned(self, etype: str, obj: Obj) -> None:
        self.stats["events"] += 1
        ref = controller_of(obj)
        if ref and ref["kind"] == self.kind:
            self.queue.add(f"{obj['metadata']['namespace']}/{ref['name']}", self.cluster.now)

    def step(self, now: int) -> None:
        if self.paused:
            return
        for informer in self.informers.values():
            informer.poll(now)
        for _ in range(len(self.queue.ready(now))):
            key = self.queue.get(now)
            if key is None:
                break
            try:
                self.stats["reconciles"] += 1
                self.reconcile(key)
            except ApiError as err:
                delay = self.queue.add_rate_limited(key, now)
                self.stats["conflicts" if isinstance(err, Conflict) else "errors"] += 1
                self.cluster.record(self.name, f"{self.kind}/{key}", err.reason, f"{err.message}; requeue in {delay} tick(s)")
            else:
                self.queue.forget(key)
            finally:
                self.queue.done(key)

    def reconcile(self, key: str) -> str:
        raise NotImplementedError


class ReplicaSetController(Controller):
    name = "replicaset-controller"
    kind = "ReplicaSet"
    owned_kinds = ("Pod",)

    def reconcile(self, key: str) -> str:
        namespace, name = split_key(key)
        try:
            rs = self.api.get("ReplicaSet", namespace, name)
        except NotFound:
            return ""
        selector = selector_from_spec(rs["spec"].get("selector"))
        pods = claim(self.api, rs, self.api.list("Pod", namespace)[0], selector)
        active = [pod for pod in pods if pod_active(pod)]
        wanted = int_of(rs["spec"].get("replicas"), 1)
        for _ in range(wanted - len(active)):
            pod = self.api.create(pod_from_template(rs))
            self.cluster.record(self.name, rs, "SuccessfulCreate", f"Created pod: {pod['metadata']['name']}")
        for pod in sorted(active, key=deletion_rank)[: max(0, len(active) - wanted)]:
            self.api.delete("Pod", namespace, pod["metadata"]["name"])
            self.cluster.record(self.name, rs, "SuccessfulDelete", f"Deleted pod: {pod['metadata']['name']}")
        ready = len([pod for pod in active if pod_ready(pod)])
        status = {"replicas": len(active), "readyReplicas": ready, "availableReplicas": ready, "observedGeneration": rs["metadata"]["generation"]}
        if rs.get("status") != status:
            updated = copy.deepcopy(rs)
            updated["status"] = status
            self.api.update_status(updated)
        return "reconciled"


def resolve_percent(value: Any, total: int, round_up: bool) -> int:
    if isinstance(value, str) and value.endswith("%"):
        scaled = total * int(value[:-1])
        return -(-scaled // 100) if round_up else scaled // 100
    return int(value)


def rolling_limits(deployment: Obj) -> Tuple[int, int]:
    spec = deployment["spec"]
    replicas = int_of(spec.get("replicas"), 1)
    rolling = (spec.get("strategy") or {}).get("rollingUpdate") or {}
    surge = resolve_percent(rolling.get("maxSurge", "25%"), replicas, True)
    unavailable = resolve_percent(rolling.get("maxUnavailable", "25%"), replicas, False)
    if surge == 0 and unavailable == 0:
        unavailable = 1
    return surge, unavailable


def revision_of(obj: Obj) -> int:
    return int_of(((obj.get("metadata") or {}).get("annotations") or {}).get(REVISION_ANNOTATION))


def spec_replicas(rs: Obj) -> int:
    return int_of(rs["spec"].get("replicas"), 1)


def status_int(obj: Obj, field: str) -> int:
    return int_of((obj.get("status") or {}).get(field))


def find_condition(status: Obj, kind: str) -> Optional[Obj]:
    return next((c for c in status.get("conditions") or [] if c.get("type") == kind), None)


class DeploymentController(Controller):
    name = "deployment-controller"
    kind = "Deployment"
    owned_kinds = ("ReplicaSet",)

    def owned_replica_sets(self, deployment: Obj) -> List[Obj]:
        namespace = deployment["metadata"]["namespace"]
        selector = selector_from_spec(deployment["spec"].get("selector"))
        return claim(self.api, deployment, self.api.list("ReplicaSet", namespace)[0], selector)

    def reconcile(self, key: str) -> str:
        namespace, name = split_key(key)
        try:
            deployment = self.api.get("Deployment", namespace, name)
        except NotFound:
            return ""
        digest = pod_template_hash(deployment["spec"]["template"])
        replica_sets = self.owned_replica_sets(deployment)
        new_rs = next((rs for rs in replica_sets if labels_of(rs).get(HASH_LABEL) == digest), None)
        old = sorted([rs for rs in replica_sets if rs is not new_rs], key=revision_of)
        latest = max([revision_of(rs) for rs in old] or [0])
        if new_rs is None:
            new_rs = self.create_replica_set(deployment, digest, latest + 1)
        elif revision_of(new_rs) <= latest:
            new_rs = self.set_revision(new_rs, latest + 1)
        if (deployment["metadata"].get("annotations") or {}).get(REVISION_ANNOTATION) != str(revision_of(new_rs)):
            deployment = self.set_revision(deployment, revision_of(new_rs))
        self.rolling_update(deployment, new_rs, old)
        self.prune_history(deployment, old)
        self.write_status(deployment, digest)
        return "reconciled"

    def create_replica_set(self, deployment: Obj, digest: str, revision: int) -> Obj:
        spec = deployment["spec"]
        template = copy.deepcopy(spec["template"])
        template.setdefault("metadata", {}).setdefault("labels", {})[HASH_LABEL] = digest
        selector = copy.deepcopy(spec["selector"])
        selector.setdefault("matchLabels", {})[HASH_LABEL] = digest
        meta = deployment["metadata"]
        return self.api.create({
            "apiVersion": "apps/v1",
            "kind": "ReplicaSet",
            "metadata": {
                "name": f"{meta['name']}-{digest}",
                "namespace": meta["namespace"],
                "labels": dict(template["metadata"]["labels"]),
                "annotations": {REVISION_ANNOTATION: str(revision)},
                "ownerReferences": [owner_reference(deployment)],
            },
            "spec": {"replicas": 0, "selector": selector, "template": template},
        })

    def set_revision(self, obj: Obj, revision: int) -> Obj:
        updated = copy.deepcopy(obj)
        updated["metadata"].setdefault("annotations", {})[REVISION_ANNOTATION] = str(revision)
        return self.api.update(updated)

    def scale(self, deployment: Obj, rs: Obj, replicas: int) -> None:
        before = spec_replicas(rs)
        updated = copy.deepcopy(rs)
        updated["spec"]["replicas"] = replicas
        self.api.update(updated)
        direction = "up" if replicas > before else "down"
        self.cluster.record(self.name, deployment, "ScalingReplicaSet", f"Scaled {direction} replica set {rs['metadata']['name']} to {replicas} from {before}")

    def rolling_update(self, deployment: Obj, new_rs: Obj, old: List[Obj]) -> None:
        replicas = int_of(deployment["spec"].get("replicas"), 1)
        surge, unavailable = rolling_limits(deployment)
        have = spec_replicas(new_rs)
        if not any(spec_replicas(rs) for rs in old) and have != replicas:
            return self.scale(deployment, new_rs, replicas)
        if have > replicas:
            return self.scale(deployment, new_rs, replicas)
        total = have + sum(spec_replicas(rs) for rs in old)
        room = replicas + surge - total
        if room > 0 and have < replicas:
            return self.scale(deployment, new_rs, min(replicas, have + room))
        budget = total - (replicas - unavailable) - (have - status_int(new_rs, "availableReplicas"))
        for rs in old:
            take = min(spec_replicas(rs), budget)
            if take > 0:
                self.scale(deployment, rs, spec_replicas(rs) - take)
                budget -= take

    def prune_history(self, deployment: Obj, old: List[Obj]) -> None:
        limit = int_of(deployment["spec"].get("revisionHistoryLimit"), 10)
        idle = [rs for rs in old if spec_replicas(rs) == 0 and status_int(rs, "replicas") == 0]
        for rs in idle[: max(0, len(idle) - limit)]:
            self.api.delete("ReplicaSet", rs["metadata"]["namespace"], rs["metadata"]["name"])
            self.cluster.record(self.name, deployment, "DeletedHistory", f"Deleted old replica set {rs['metadata']['name']} (revisionHistoryLimit {limit})")

    def write_status(self, deployment: Obj, digest: str) -> None:
        now = self.cluster.now
        owned = self.owned_replica_sets(deployment)
        new_rs = next((rs for rs in owned if labels_of(rs).get(HASH_LABEL) == digest), None)
        desired = int_of(deployment["spec"].get("replicas"), 1)
        counts = [sum(status_int(rs, f) for rs in owned) for f in ("replicas", "readyReplicas", "availableReplicas")]
        total, ready, available = counts
        updated = status_int(new_rs, "replicas") if new_rs else 0
        previous = deployment.get("status") or {}
        before = [previous.get(f, 0) for f in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")]
        progress = find_condition(previous, "Progressing")
        moved = before != [total, updated, ready, available] or previous.get("observedGeneration") != deployment["metadata"]["generation"]
        since = now if moved or not progress else progress.get("lastUpdateTick", now)
        subject = new_rs["metadata"]["name"] if new_rs else digest
        deadline = int_of(deployment["spec"].get("progressDeadlineSeconds"), 600)
        if updated == desired and total == desired and available == desired:
            progressing = ("True", "NewReplicaSetAvailable", f'ReplicaSet "{subject}" has successfully progressed.')
        elif now - since > deadline:
            progressing = ("False", "ProgressDeadlineExceeded", f'ReplicaSet "{subject}" has timed out progressing.')
        else:
            progressing = ("True", "ReplicaSetUpdated", f'ReplicaSet "{subject}" is progressing.')
            self.queue.add_after(key_of(deployment), max(1, deadline - (now - since) + 1), now)
        minimum = available >= desired - rolling_limits(deployment)[1]
        availability = ("True", "MinimumReplicasAvailable", "Deployment has minimum availability.") if minimum else ("False", "MinimumReplicasUnavailable", "Deployment does not have minimum availability.")
        status = {
            "observedGeneration": deployment["metadata"]["generation"],
            "replicas": total,
            "updatedReplicas": updated,
            "readyReplicas": ready,
            "availableReplicas": available,
            "unavailableReplicas": max(0, desired - available),
            "conditions": [
                {"type": "Available", "status": availability[0], "reason": availability[1], "message": availability[2]},
                {"type": "Progressing", "status": progressing[0], "reason": progressing[1], "message": progressing[2], "lastUpdateTick": since},
            ],
        }
        if deployment.get("status") != status:
            changed = copy.deepcopy(deployment)
            changed["status"] = status
            self.api.update_status(changed)


class FakeKubelet:
    def __init__(self, cluster: "Cluster"):
        self.cluster = cluster

    def ready_after(self, pod: Obj) -> Optional[int]:
        if any(str(c.get("image", "")).endswith(":broken") for c in pod["spec"].get("containers") or []):
            return None
        annotation = (pod["metadata"].get("annotations") or {}).get(READY_AFTER_ANNOTATION)
        return int(annotation) if annotation is not None else self.cluster.ready_after

    def step(self, now: int) -> None:
        for pod in self.cluster.api.list("Pod")[0]:
            if not pod_active(pod):
                continue
            age = now - pod["metadata"]["creationTimestamp"]
            delay = self.ready_after(pod)
            wanted = pod_status("Running" if age >= 1 else "Pending", delay is not None and age >= delay)
            if pod.get("status") != wanted:
                updated = copy.deepcopy(pod)
                updated["status"] = wanted
                try:
                    self.cluster.api.update_status(updated)
                except ApiError:
                    continue
                if pod_ready(updated) and not pod_ready(pod):
                    self.cluster.record("kubelet", pod, "Ready", "container is ready")


def update_with_retry(api: ApiServer, kind: str, namespace: str, name: str, mutate: Callable[[Obj], None], attempts: int = 5) -> Obj:
    for attempt in range(attempts):
        obj = api.get(kind, namespace, name)
        mutate(obj)
        try:
            return api.update(obj)
        except Conflict:
            if attempt == attempts - 1:
                raise
    raise AssertionError("unreachable")


class Kubectl:
    def __init__(self, cluster: "Cluster"):
        self.cluster = cluster
        self.api = cluster.api

    def apply(self, obj: Obj) -> Obj:
        meta = obj["metadata"]
        try:
            current = self.api.get(obj["kind"], meta.get("namespace", "default"), meta["name"])
        except NotFound:
            return self.api.create(obj)
        replacement = copy.deepcopy(obj)
        replacement["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
        return self.api.update(replacement)

    def pods(self, app: str) -> List[Obj]:
        return self.api.list("Pod", "default", f"app={app}")[0]

    def ready_pods(self, app: str) -> List[Obj]:
        return [pod for pod in self.pods(app) if pod_active(pod) and pod_ready(pod)]

    def log(self, subject: str, reason: str, message: str) -> None:
        self.cluster.record("kubectl", subject, reason, message)

    def delete_pod(self, name: str) -> None:
        self.api.delete("Pod", "default", name)
        self.log(f"Pod/default/{name}", "Delete", f"kubectl delete pod {name}")

    def scale(self, name: str, replicas: int) -> Obj:
        def mutate(deployment: Obj) -> None:
            deployment["spec"]["replicas"] = replicas
        updated = update_with_retry(self.api, "Deployment", "default", name, mutate)
        self.log(f"Deployment/default/{name}", "Scale", f"kubectl scale deployment {name} --replicas={replicas}")
        return updated

    def set_image(self, name: str, image: str) -> Obj:
        def mutate(deployment: Obj) -> None:
            deployment["spec"]["template"]["spec"]["containers"][0]["image"] = image
        updated = update_with_retry(self.api, "Deployment", "default", name, mutate)
        self.log(f"Deployment/default/{name}", "SetImage", f"kubectl set image deployment/{name} app={image}")
        return updated

    def history(self, name: str) -> List[Obj]:
        deployment = self.api.get("Deployment", "default", name)
        owned = [rs for rs in self.api.list("ReplicaSet", "default")[0] if (controller_of(rs) or {}).get("uid") == deployment["metadata"]["uid"]]
        return [
            {"revision": revision_of(rs), "replicaset": rs["metadata"]["name"], "hash": labels_of(rs).get(HASH_LABEL), "image": rs["spec"]["template"]["spec"]["containers"][0]["image"]}
            for rs in sorted(owned, key=revision_of)
        ]

    def undo(self, name: str, revision: Optional[int] = None) -> Obj:
        history = self.history(name)
        if revision is None and len(history) < 2:
            raise Invalid(f'no rollout history found for deployment "{name}"')
        wanted = revision if revision is not None else history[-2]["revision"]
        target = next((entry for entry in history if entry["revision"] == wanted), None)
        if target is None:
            raise Invalid(f"unable to find specified revision {wanted} in history")
        rs = self.api.get("ReplicaSet", "default", target["replicaset"])

        def mutate(deployment: Obj) -> None:
            template = copy.deepcopy(rs["spec"]["template"])
            template["metadata"]["labels"].pop(HASH_LABEL, None)
            deployment["spec"]["template"] = template
        updated = update_with_retry(self.api, "Deployment", "default", name, mutate)
        self.log(f"Deployment/default/{name}", "Undo", f"kubectl rollout undo deployment/{name} --to-revision={wanted}")
        return updated

    def rollout_status(self, name: str) -> Tuple[bool, str]:
        deployment = self.api.get("Deployment", "default", name)
        spec, status = deployment["spec"], deployment.get("status") or {}
        progress = find_condition(status, "Progressing")
        if progress and progress.get("reason") == "ProgressDeadlineExceeded":
            return False, f'deployment "{name}" exceeded its progress deadline'
        if deployment["metadata"]["generation"] > int_of(status.get("observedGeneration")):
            return False, "Waiting for deployment spec update to be observed..."
        desired, updated = int_of(spec.get("replicas"), 1), int_of(status.get("updatedReplicas"))
        total, available = int_of(status.get("replicas")), int_of(status.get("availableReplicas"))
        waiting = f'Waiting for deployment "{name}" rollout to finish:'
        if updated < desired:
            return False, f"{waiting} {updated} out of {desired} new replicas have been updated..."
        if total > updated:
            return False, f"{waiting} {total - updated} old replicas are pending termination..."
        if available < updated:
            return False, f"{waiting} {available} of {updated} updated replicas are available..."
        return True, f'deployment "{name}" successfully rolled out'


class Cluster:
    def __init__(self, ready_after: int = 2, resync_period: int = 10, backoff_base: int = 1, backoff_cap: int = 32):
        self.now = 0
        self.ready_after = ready_after
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.api = ApiServer(lambda: self.now)
        self.events: List[Obj] = []
        self.observers: List[Callable[[], None]] = []
        self.kubelet = FakeKubelet(self)
        self.deployments = DeploymentController(self, resync_period)
        self.replicasets = ReplicaSetController(self, resync_period)
        self.kubectl = Kubectl(self)

    def record(self, source: str, subject: Union[str, Obj], reason: str, message: str) -> None:
        self.events.append({"tick": self.now, "source": source, "object": subject if isinstance(subject, str) else ref_of(subject), "reason": reason, "message": message})

    def tick(self) -> None:
        self.now += 1
        self.kubelet.step(self.now)
        self.deployments.step(self.now)
        self.replicasets.step(self.now)
        for kind, namespace, name in self.api.garbage_collect():
            self.record("garbage-collector", f"{kind}/{namespace}/{name}", "Deleted", "owner no longer exists")
        for observer in self.observers:
            observer()

    def run(self, ticks: int) -> None:
        for _ in range(ticks):
            self.tick()

    def run_until(self, predicate: Callable[[], bool], limit: int = 80, what: str = "condition") -> int:
        for elapsed in range(limit):
            if predicate():
                return elapsed
            self.tick()
        if predicate():
            return limit
        raise Stalled(f"{what} not reached after {limit} ticks (tick {self.now})")

    def stats(self) -> Dict[str, Dict[str, int]]:
        return {c.name: dict(c.stats) for c in (self.deployments, self.replicasets)}


def deployment_manifest(name: str, image: str, replicas: int, max_surge: Any = 1, max_unavailable: Any = 0, deadline: int = 600, history: int = 10) -> Obj:
    labels = {"app": name}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": "default"},
        "spec": {
            "replicas": replicas,
            "revisionHistoryLimit": history,
            "progressDeadlineSeconds": deadline,
            "selector": {"matchLabels": labels},
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": max_surge, "maxUnavailable": max_unavailable}},
            "template": {"metadata": {"labels": dict(labels)}, "spec": {"containers": [{"name": "app", "image": image}]}},
        },
    }


def serve(cluster: Cluster, name: str, image: str, replicas: int, **options: Any) -> int:
    cluster.kubectl.apply(deployment_manifest(name, image, replicas, **options))
    return cluster.run_until(lambda: len(cluster.kubectl.ready_pods(name)) == replicas, what=f"{replicas} ready pods for {name}")


def names(pods: List[Obj]) -> List[str]:
    return sorted(pod["metadata"]["name"] for pod in pods)


def scenario_self_heal(cluster: Cluster) -> Obj:
    k = cluster.kubectl
    created = serve(cluster, "web", "web:1.0", 3)
    owner = controller_of(k.pods("web")[0])["name"]
    before = names(k.pods("web"))
    victim = before[0]
    k.delete_pod(victim)
    healed = cluster.run_until(lambda: len(k.ready_pods("web")) == 3, what="the replacement to be ready")
    replacement = sorted(set(names(k.pods("web"))) - set(before))
    k.scale("web", 5)
    cluster.run_until(lambda: len(k.ready_pods("web")) == 5, what="5 ready pods")
    k.scale("web", 2)
    cluster.run_until(lambda: len(k.pods("web")) == 2, what="2 pods")
    return {
        "ticks_to_ready": created,
        "ticks_to_heal": healed,
        "victim": victim,
        "victim_replaced": victim not in names(k.pods("web")),
        "same_replicaset": all(controller_of(p)["name"] == owner for p in k.pods("web")),
        "pods_after_scale_down": names(k.pods("web")),
        "replacement": replacement,
    }


def scenario_rolling(cluster: Cluster) -> Obj:
    k = cluster.kubectl
    serve(cluster, "web", "web:1.0", 3, max_surge=1, max_unavailable=0)
    samples: List[Tuple[int, int]] = []
    cluster.observers.append(lambda: samples.append((len([p for p in k.pods("web") if pod_active(p)]), len(k.ready_pods("web")))))
    k.set_image("web", "web:2.0")
    ticks = cluster.run_until(lambda: k.rollout_status("web")[0] and len(k.pods("web")) == 3, what="the rollout")
    cluster.observers.clear()
    return {
        "rollout_ticks": ticks,
        "max_pods": max(total for total, _ in samples),
        "min_ready": min(ready for _, ready in samples),
        "revisions": [entry["revision"] for entry in k.history("web")],
        "images": sorted({p["spec"]["containers"][0]["image"] for p in k.pods("web")}),
        "status": k.rollout_status("web")[1],
    }


def scenario_rollback(cluster: Cluster) -> Obj:
    k = cluster.kubectl
    serve(cluster, "web", "web:1.0", 3, deadline=6)
    first = k.history("web")[0]["replicaset"]
    samples: List[int] = []
    cluster.observers.append(lambda: samples.append(len(k.ready_pods("web"))))
    k.set_image("web", "web:broken")
    cluster.run_until(lambda: not k.rollout_status("web")[0] and "deadline" in k.rollout_status("web")[1], limit=40, what="the progress deadline")
    stuck = k.rollout_status("web")[1]
    before = [entry["revision"] for entry in k.history("web")]
    pods_while_stuck = len([p for p in k.pods("web") if pod_active(p)])
    k.undo("web")
    cluster.run_until(lambda: k.rollout_status("web")[0] and len(k.pods("web")) == 3, what="the rollback")
    cluster.observers.clear()
    history = k.history("web")
    return {
        "stuck_message": stuck,
        "pods_while_stuck": pods_while_stuck,
        "min_ready": min(samples),
        "history_before_undo": before,
        "history_after_undo": [entry["revision"] for entry in history],
        "reused_first_replicaset": history[-1]["replicaset"] == first,
        "image_after": history[-1]["image"],
    }


def scenario_conflict(cluster: Cluster) -> Obj:
    k, api = cluster.kubectl, cluster.api
    k.apply(deployment_manifest("web", "web:1.0", 2))
    mine, theirs = api.get("Deployment", "default", "web"), api.get("Deployment", "default", "web")
    mine["spec"]["replicas"] = 3
    api.update(mine)
    theirs["spec"]["replicas"] = 4
    try:
        api.update(theirs)
        message = ""
    except Conflict as err:
        message = err.message
    retried = k.scale("web", 4)
    fired: List[str] = []

    def interfere(operation: str, kind: str, namespace: str, name: str) -> None:
        if operation == "status" and kind == "ReplicaSet" and not fired:
            fired.append(name)
            rs = api.get(kind, namespace, name)
            rs["metadata"].setdefault("annotations", {})["touched-by"] = "someone else"
            api.update(rs)

    api.hooks.append(interfere)
    cluster.run_until(lambda: len(k.ready_pods("web")) == 4, what="4 ready pods")
    api.hooks.remove(interfere)
    return {
        "stale_update_error": message,
        "retry_replicas": retried["spec"]["replicas"],
        "controller_conflicts": cluster.replicasets.stats["conflicts"],
        "ready_pods": len(k.ready_pods("web")),
    }


def scenario_quarantine(cluster: Cluster) -> Obj:
    k, api = cluster.kubectl, cluster.api
    serve(cluster, "web", "web:1.0", 3)
    sick = api.get("Pod", "default", names(k.pods("web"))[0])
    sick["metadata"]["labels"].pop("app")
    api.update(sick)
    cluster.run_until(lambda: len(k.ready_pods("web")) == 3, what="a replacement for the relabelled pod")
    kept = api.get("Pod", "default", sick["metadata"]["name"])
    return {
        "quarantined": sick["metadata"]["name"],
        "quarantined_owner": controller_of(kept),
        "quarantined_still_running": pod_phase(kept) == "Running",
        "serving_pods": names(k.pods("web")),
    }


def scenario_gc(cluster: Cluster) -> Obj:
    k, api = cluster.kubectl, cluster.api
    serve(cluster, "web", "web:1.0", 2)
    serve(cluster, "db", "db:1.0", 1)
    api.delete("Deployment", "default", "web")
    api.delete("Deployment", "default", "db", orphan=True)
    cluster.run(3)
    left = [(obj["kind"], obj["metadata"]["name"]) for kind in ("ReplicaSet", "Pod") for obj in api.list(kind)[0]]
    return {
        "web_objects_left": len([1 for _, n in left if n.startswith("web-")]),
        "db_objects_left": sorted(f"{kind}/{n}" for kind, n in left if n.startswith("db-")),
        "db_owners": [controller_of(o) for kind in ("ReplicaSet", "Pod") for o in api.list(kind)[0] if o["metadata"]["name"].startswith("db-")],
    }


def scenario_resync(cluster: Cluster) -> Obj:
    k = cluster.kubectl
    serve(cluster, "web", "web:1.0", 3)
    cluster.run(3)
    informer = cluster.replicasets.informers["Pod"]
    informer.drop = True
    deleted_at = cluster.now
    k.delete_pod(names(k.pods("web"))[0])
    cluster.run(3)
    blind = len(k.pods("web"))
    cluster.run_until(lambda: len(k.ready_pods("web")) == 3, what="the resync to notice the missing pod")
    informer.drop = False
    return {"pods_while_blind": blind, "deleted_at_tick": deleted_at, "healed_at_tick": cluster.now, "resync_period": cluster.replicasets.informers["ReplicaSet"].resync_period}


def scenario_relist(cluster: Cluster) -> Obj:
    k = cluster.kubectl
    serve(cluster, "web", "web:1.0", 3)
    controller = cluster.replicasets
    controller.paused = True
    for name in names(k.pods("web"))[:2]:
        k.delete_pod(name)
    cluster.api.compact()
    cluster.run(1)
    controller.paused = False
    cluster.run_until(lambda: len(k.ready_pods("web")) == 3, what="the relist to recreate the pods")
    informer = controller.informers["Pod"]
    return {"relists": informer.lists, "gone": informer.last_gone, "ready_pods": len(k.ready_pods("web"))}


SCENARIOS: Dict[str, Tuple[Callable[[Cluster], Obj], str]] = {
    "self-heal": (scenario_self_heal, "delete a pod and the ReplicaSet replaces it, then scale up and down"),
    "rolling": (scenario_rolling, "a rolling update with maxSurge=1 and maxUnavailable=0 never dips below 3 ready pods"),
    "rollback": (scenario_rollback, "a broken image stalls the rollout without an outage; undo reuses the old ReplicaSet"),
    "conflict": (scenario_conflict, "a stale update gets 409; a controller write that loses a race is requeued with backoff"),
    "quarantine": (scenario_quarantine, "relabel a pod out of the selector: it is released and a replacement appears"),
    "gc": (scenario_gc, "delete an owner: dependents are collected, or orphaned with --cascade=orphan"),
    "resync": (scenario_resync, "a controller that lost every pod event heals at the next resync"),
    "relist": (scenario_relist, "a controller that was down past compaction gets 410 Gone and relists"),
}


def describe_objects(api: ApiServer) -> List[Obj]:
    rows = []
    for kind in ("Deployment", "ReplicaSet", "Pod"):
        for obj in api.list(kind)[0]:
            owner = controller_of(obj)
            row = {"kind": kind, "name": obj["metadata"]["name"], "owner": f"{owner['kind']}/{owner['name']}" if owner else ""}
            if kind == "Pod":
                row.update(ready="1/1" if pod_ready(obj) else "0/1", status=pod_phase(obj))
            else:
                row.update(ready=f"{status_int(obj, 'readyReplicas')}/{int_of(obj['spec'].get('replicas'), 1)}", status="")
            rows.append(row)
    return rows


def format_table(headers: List[str], rows: List[List[str]]) -> List[str]:
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    return ["  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in [headers] + rows]


def format_event(event: Obj) -> str:
    return f"t={event['tick']:03d}  {event['source']:<21} {event['object']:<38} {event['reason']:<18} {event['message']}"


def cmd_run(args: argparse.Namespace) -> int:
    if args.scenario not in SCENARIOS:
        print(f"minictl: unknown scenario {args.scenario!r}; choose from: {', '.join(SCENARIOS)}", file=sys.stderr)
        return 2
    cluster = Cluster(ready_after=args.ready_after, resync_period=args.resync)
    function, description = SCENARIOS[args.scenario]
    summary = function(cluster)
    cluster.run(3)
    result = {"scenario": args.scenario, "description": description, "ticks": cluster.now, "events": cluster.events, "objects": describe_objects(cluster.api), "stats": cluster.stats(), "summary": summary}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"scenario {args.scenario}: {description}")
    print("\n".join(format_event(e) for e in cluster.events))
    print()
    print("\n".join(format_table(["NAME", "OWNER", "READY", "STATUS"], [[f"{r['kind'].lower()}/{r['name']}", r["owner"], r["ready"], r["status"]] for r in result["objects"]])))
    print("\nsummary")
    for key, value in summary.items():
        print(f"  {key}: {json.dumps(value)}")
    print(f"\n{cluster.now} ticks; " + "; ".join(f"{name}: {s['events']} events -> {s['reconciles']} reconciles, {s['conflicts']} conflicts" for name, s in result["stats"].items()))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps([{"name": n, "description": d} for n, (_, d) in SCENARIOS.items()], indent=2))
    else:
        print("\n".join(format_table(["SCENARIO", "WHAT IT SHOWS"], [[n, d] for n, (_, d) in SCENARIOS.items()])))
    return 0


def cmd_select(args: argparse.Namespace) -> int:
    text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
    data = json.loads(text)
    items = data["items"] if isinstance(data, dict) and "items" in data else [data]
    requirements = parse_selector(args.selector)
    matched = sorted(item["metadata"]["name"] for item in items if selector_matches(requirements, labels_of(item)))
    if args.json:
        print(json.dumps({"selector": args.selector, "matched": matched}))
    elif matched:
        print("\n".join(matched))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minictl", description="Controllers from scratch: an API store, a work queue, ReplicaSet and Deployment reconcilers.")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="run a scenario and print its event log")
    run.add_argument("scenario")
    run.add_argument("--ready-after", type=int, default=2, help="ticks until a fake pod is Ready")
    run.add_argument("--resync", type=int, default=10, help="ticks between periodic resyncs")
    run.add_argument("--json", action="store_true")
    listing = sub.add_parser("list", help="list scenarios")
    listing.add_argument("--json", action="store_true")
    select = sub.add_parser("select", help="evaluate a label selector against a kubectl JSON list")
    select.add_argument("selector")
    select.add_argument("--file", required=True, help="kubectl get ... -o json output, or - for stdin")
    select.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    if args.command is None:
        parser.print_help()
        return 2
    try:
        return {"run": cmd_run, "list": cmd_list, "select": cmd_select}[args.command](args)
    except Invalid as err:
        print(f"minictl: {err.reason}: {err.message}", file=sys.stderr)
        return 2
    except (ApiError, Stalled) as err:
        print(f"minictl: {getattr(err, 'message', err)}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as err:
        print(f"minictl: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
