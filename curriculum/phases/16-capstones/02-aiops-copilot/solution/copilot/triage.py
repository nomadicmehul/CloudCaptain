from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .evidence import collect, meta
from .kube import Kube

RULES: Sequence[Tuple[str, set, set]] = (
    ("oom", {"OOMKilled"}, {"Restarts"}),
    ("image-pull", {"ErrImagePull", "ImagePullBackOff", "InvalidImageName"}, {"Failed"}),
    ("crashloop-config", {"CrashLoopBackOff", "Error", "CreateContainerConfigError", "CreateContainerError", "RunContainerError", "BackOff"}, {"Logs", "Restarts", "BackOff"}),
    ("no-endpoints", {"NoEndpoints", "NotReady"}, {"Unhealthy"}),
)
CONFIG_HINT = re.compile(r"(?i)(invalid|missing|required|not set|config|KeyError|unknown option|parse)")
WORKLOAD_KINDS = ("deployments", "statefulsets", "daemonsets")


def classify(items: Iterable[Dict[str, Any]]) -> Tuple[str, List[str], str]:
    items = list(items)
    for category, triggers, supporting in RULES:
        hits = [i for i in items if i.get("signal") in triggers]
        if hits:
            cited = [i["id"] for i in items if i.get("signal") in triggers or i.get("signal") in supporting]
            return category, cited, f"{hits[0].get('object')}: {hits[0].get('signal')} ({hits[0].get('summary')})"
    return "healthy", [], "no unhealthy signal"


def config_line(items: Iterable[Dict[str, Any]]) -> Optional[str]:
    for entry in items:
        if entry.get("signal") == "Logs":
            for line in reversed(entry.get("data", {}).get("lines") or []):
                if CONFIG_HINT.search(line):
                    return line.strip()
    return None


def group(evidence: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for entry in evidence:
        grouped.setdefault(entry["workload"], []).append(entry)
    return grouped


def triage_evidence(evidence: Sequence[Dict[str, Any]], workloads: Iterable[str] = ()) -> List[Dict[str, Any]]:
    grouped = group(evidence)
    names = list(dict.fromkeys([*workloads, *grouped]))
    incidents = []
    for workload in names:
        category, citations, reason = classify(grouped.get(workload, []))
        incidents.append({"workload": workload, "category": category, "citations": citations, "reason": reason})
    return sorted(incidents, key=lambda i: (i["category"] == "healthy", i["workload"]))


def list_workloads(kube: Kube, namespace: str) -> List[str]:
    found = []
    for kind in WORKLOAD_KINDS:
        for obj in kube.items("get", kind, "-n", namespace):
            found.append(f"{kind[:-1]}/{meta(obj).get('name')}")
    return found


def triage(kube: Kube, namespace: str) -> Dict[str, Any]:
    collected = collect(kube, namespace)
    incidents = triage_evidence(collected["evidence"], list_workloads(kube, namespace))
    return {"namespace": namespace, "incidents": incidents, "evidence": collected["evidence"]}
