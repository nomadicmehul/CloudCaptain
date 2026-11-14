from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from .guard import redact
from .kube import Kube, KubeError

WAITING_SIGNALS = {"CrashLoopBackOff", "ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError", "CreateContainerError", "RunContainerError"}
LOG_TAIL = 30
UNAVAILABLE = "unable to retrieve container logs"


def item(item_id: str, kind: str, workload: str, obj: str, signal: str, summary: str, time: Optional[str] = None, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"id": item_id, "kind": kind, "workload": workload, "object": obj, "signal": signal, "summary": summary, "time": time, "data": data or {}}


def meta(obj: Dict[str, Any]) -> Dict[str, Any]:
    return obj.get("metadata") or {}


def replicaset_owners(replicasets: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    owners = {}
    for rs in replicasets:
        for ref in meta(rs).get("ownerReferences") or []:
            if ref.get("controller"):
                owners[meta(rs).get("name")] = f"{str(ref.get('kind')).lower()}/{ref.get('name')}"
    return owners


def workload_of(pod: Dict[str, Any], rs_owners: Dict[str, str]) -> str:
    for ref in meta(pod).get("ownerReferences") or []:
        if not ref.get("controller"):
            continue
        if ref.get("kind") == "ReplicaSet":
            return rs_owners.get(ref.get("name"), f"replicaset/{ref.get('name')}")
        return f"{str(ref.get('kind')).lower()}/{ref.get('name')}"
    return f"pod/{meta(pod).get('name')}"


def terminated_item(pod: str, container: str, workload: str, terminated: Dict[str, Any], slot: str) -> Optional[Dict[str, Any]]:
    reason = str(terminated.get("reason") or "")
    code = int(terminated.get("exitCode") or 0)
    if reason != "OOMKilled" and code == 0:
        return None
    signal = "OOMKilled" if reason == "OOMKilled" else "Error"
    summary = f"{container} {'was OOMKilled' if signal == 'OOMKilled' else 'exited'} with code {code}" + (f" ({reason})" if reason and reason not in ("OOMKilled", "Error") else "")
    return item(f"pod/{pod}/{container}/{slot}", "container-state", workload, f"pod/{pod}", signal, summary, terminated.get("finishedAt"), {"exit_code": code, "reason": reason, "slot": slot})


def container_limits(pod: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {c.get("name"): ((c.get("resources") or {}).get("limits") or {}) for c in (pod.get("spec") or {}).get("containers") or []}


def pod_items(pod: Dict[str, Any], workload: str) -> Tuple[List[Dict[str, Any]], List[Tuple[str, bool]]]:
    name = meta(pod).get("name")
    status = pod.get("status") or {}
    limits = container_limits(pod)
    found: List[Dict[str, Any]] = []
    logs: List[Tuple[str, bool]] = []
    for cs in status.get("containerStatuses") or []:
        container = cs.get("name")
        state = cs.get("state") or {}
        last = cs.get("lastState") or {}
        restarts = int(cs.get("restartCount") or 0)
        before = len(found)
        waiting = state.get("waiting") or {}
        if waiting.get("reason") in WAITING_SIGNALS:
            found.append(item(f"pod/{name}/{container}/state", "container-state", workload, f"pod/{name}", waiting["reason"], redact(str(waiting.get("message") or waiting["reason"]))[:300], None, {"image": cs.get("image")}))
        for slot, source in (("state", state), ("last-state", last)):
            if source.get("terminated"):
                entry = terminated_item(name, container, workload, source["terminated"], slot)
                if entry:
                    entry["data"]["memory_limit"] = limits.get(container, {}).get("memory")
                    found.append(entry)
        if restarts and not cs.get("ready"):
            found.append(item(f"pod/{name}/{container}/restarts", "restarts", workload, f"pod/{name}", "Restarts", f"{container} restarted {restarts} times", None, {"restart_count": restarts}))
        if len(found) > before and (restarts or state.get("terminated")):
            logs.append((container, "running" in state))
    conditions = {c.get("type"): c for c in status.get("conditions") or []}
    running = any("running" in (cs.get("state") or {}) for cs in status.get("containerStatuses") or [])
    ready = conditions.get("Ready") or {}
    if status.get("phase") == "Running" and running and ready.get("status") == "False" and not found:
        found.append(item(f"pod/{name}/ready", "pod-condition", workload, f"pod/{name}", "NotReady", f"running but not ready: {ready.get('reason') or 'readiness probe failing'}", ready.get("lastTransitionTime"), {"message": ready.get("message")}))
    return found, logs


def ready_endpoints(slices: Iterable[Dict[str, Any]]) -> Tuple[int, int]:
    ready = total = 0
    for endpoint_slice in slices:
        for endpoint in endpoint_slice.get("endpoints") or []:
            total += 1
            if (endpoint.get("conditions") or {}).get("ready") is not False:
                ready += 1
    return ready, total


def service_items(services: Iterable[Dict[str, Any]], slices: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_service: Dict[str, List[Dict[str, Any]]] = {}
    for endpoint_slice in slices:
        owner = (meta(endpoint_slice).get("labels") or {}).get("kubernetes.io/service-name")
        if owner:
            by_service.setdefault(owner, []).append(endpoint_slice)
    found = []
    for service in services:
        spec = service.get("spec") or {}
        name = meta(service).get("name")
        if not spec.get("selector") or spec.get("type") == "ExternalName":
            continue
        ready, total = ready_endpoints(by_service.get(name, []))
        if ready == 0:
            found.append(item(f"service/{name}/endpoints", "endpoints", f"service/{name}", f"service/{name}", "NoEndpoints", f"service {name} has 0 ready endpoints ({total} not ready)", None, {"ready": 0, "not_ready": total, "selector": spec.get("selector")}))
    return found


def event_time(event: Dict[str, Any]) -> Optional[str]:
    return event.get("lastTimestamp") or event.get("eventTime") or meta(event).get("creationTimestamp")


def event_items(events: Iterable[Dict[str, Any]], unhealthy: Dict[str, str], healthy: Iterable[str]) -> List[Dict[str, Any]]:
    healthy = set(healthy)
    latest: Dict[str, Dict[str, Any]] = {}
    for event in sorted(events, key=lambda e: event_time(e) or ""):
        if event.get("type") != "Warning":
            continue
        involved = event.get("involvedObject") or {}
        kind, name = involved.get("kind"), involved.get("name")
        if kind == "Pod":
            if name not in unhealthy:
                continue
            workload = unhealthy[name]
        elif name in healthy:
            continue
        else:
            workload = f"{str(kind).lower()}/{name}"
        reason = str(event.get("reason") or "Warning")
        entry = item(f"event/{name}/{reason}", "event", workload, f"{str(kind).lower()}/{name}", reason, redact(str(event.get("message") or ""))[:300], event_time(event), {"count": event.get("count") or 1})
        latest[entry["id"]] = entry
    return list(latest.values())


def usable_log(code: int, out: str) -> bool:
    return code == 0 and bool(out.strip()) and not out.lstrip().startswith(UNAVAILABLE)


def log_item(kube: Kube, namespace: str, pod: str, container: str, workload: str, running: bool) -> Optional[Dict[str, Any]]:
    args = ["logs", pod, "-n", namespace, "-c", container, f"--tail={LOG_TAIL}"]
    for previous in ((True, False) if running else (False, True)):
        code, out, _ = kube.run(*args, *(["--previous"] if previous else []))
        if usable_log(code, out):
            lines = [redact(line) for line in out.splitlines()[-LOG_TAIL:]]
            return item(f"pod/{pod}/{container}/logs", "logs", workload, f"pod/{pod}", "Logs", f"last {len(lines)} log lines of the {'previous' if previous else 'latest'} {container} container", None, {"previous": previous, "lines": lines})
    return None


def collect(kube: Kube, namespace: str) -> Dict[str, Any]:
    pods = kube.items("get", "pods", "-n", namespace)
    replicasets = kube.items("get", "replicasets", "-n", namespace)
    services = kube.items("get", "services", "-n", namespace)
    slices = kube.items("get", "endpointslices.discovery.k8s.io", "-n", namespace)
    try:
        events = kube.items("get", "events", "-n", namespace)
    except KubeError:
        events = []
    owners = replicaset_owners(replicasets)
    evidence: List[Dict[str, Any]] = []
    unhealthy: Dict[str, str] = {}
    healthy: List[str] = []
    for pod in sorted(pods, key=lambda p: meta(p).get("name") or ""):
        if meta(pod).get("deletionTimestamp"):
            continue
        name = meta(pod).get("name")
        workload = workload_of(pod, owners)
        found, logs = pod_items(pod, workload)
        if not found:
            healthy.append(name)
            continue
        unhealthy[name] = workload
        evidence.extend(found)
        for container, running in logs:
            entry = log_item(kube, namespace, name, container, workload, running)
            if entry:
                evidence.append(entry)
    evidence.extend(service_items(services, slices))
    evidence.extend(event_items(events, unhealthy, healthy))
    return {"namespace": namespace, "evidence": evidence}
