from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .evidence import collect, meta
from .kube import Kube
from .proposals import REVISION, ProposalStore, plan
from .triage import config_line, group, list_workloads, triage_evidence

CONFIDENCE = {"oom": 0.9, "image-pull": 0.9, "crashloop-config": 0.8, "no-endpoints": 0.75}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def first(items: List[Dict[str, Any]], *signals: str) -> Optional[Dict[str, Any]]:
    return next((entry for entry in items if entry.get("signal") in signals), None)


def hypothesis_for(category: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if category == "oom":
        hit = first(items, "OOMKilled") or {}
        limit = (hit.get("data") or {}).get("memory_limit") or "its limit"
        return {"hypothesis": f"the container needs more memory than {limit} and the kernel OOM-kills it on every start", "confidence": CONFIDENCE[category]}
    if category == "image-pull":
        hit = first(items, "ErrImagePull", "ImagePullBackOff", "InvalidImageName") or {}
        image = (hit.get("data") or {}).get("image") or "the image"
        return {"hypothesis": f"{image} cannot be pulled: the tag does not exist or the registry is unreachable", "confidence": CONFIDENCE[category]}
    if category == "crashloop-config":
        line = config_line(items)
        if line:
            return {"hypothesis": f"the process exits at start on a configuration error: {line}", "confidence": CONFIDENCE[category]}
        return {"hypothesis": "the process exits at start; the logs do not name the cause", "confidence": 0.5}
    return {"hypothesis": "pods run but fail their readiness probe, so the Service has no ready endpoints and requests fail", "confidence": CONFIDENCE["no-endpoints"]}


def action_id(*parts: str) -> str:
    return "a-" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:10]


def manual(workload: str, category: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    name = workload.split("/", 1)[-1]
    if category == "oom":
        limit = ((first(items, "OOMKilled") or {}).get("data") or {}).get("memory_limit") or "the current limit"
        text, risk, command = f"raise resources.limits.memory above {limit} or reduce the working set, then roll out", "medium", f"kubectl set resources deployment/{name} --limits=memory=<new limit>"
    elif category == "image-pull":
        text, risk, command = "point the Deployment at an image tag that exists (or fix registry credentials)", "low", f"kubectl set image deployment/{name} <container>=<image:existing-tag>"
    elif category == "crashloop-config":
        text, risk, command = "correct the configuration value named in the logs (ConfigMap or env), then restart the Deployment", "low", f"kubectl edit configmap <name> && kubectl rollout restart deployment/{name}"
    else:
        text, risk, command = "fix the readiness probe or the endpoint it calls so pods become Ready", "medium", f"kubectl edit deployment/{name}"
    return {"id": action_id(workload, category, text), "action": "manual", "target": workload, "risk": risk, "status": "proposed", "reason": text, "command": command}


def actions_for(kube: Kube, store: ProposalStore, namespace: str, workload: str, category: str, items: List[Dict[str, Any]], revisions: Dict[str, int]) -> List[Dict[str, Any]]:
    found = [manual(workload, category, items)]
    name = workload.split("/", 1)[-1]
    if workload.startswith("deployment/") and category in ("crashloop-config", "image-pull") and revisions.get(name, 0) > 1:
        proposal = plan(kube, store, namespace, "rollback", name, reason=f"{category}: return to the previous revision")
        summary = {key: proposal[key] for key in ("id", "action", "risk", "status", "reason", "diff")}
        found.insert(0, {**summary, "target": workload, "command": " ".join(proposal["command"])})
    return found


def build_report(kube: Kube, store: ProposalStore, namespace: str, now: str = "") -> Dict[str, Any]:
    collected = collect(kube, namespace)
    evidence = collected["evidence"]
    workloads = list_workloads(kube, namespace)
    incidents = triage_evidence(evidence, workloads)
    grouped = group(evidence)
    revisions = {meta(d).get("name"): int((meta(d).get("annotations") or {}).get(REVISION) or 0) for d in kube.items("get", "deployments", "-n", namespace)}
    unhealthy = [i for i in incidents if i["category"] != "healthy"]
    hypotheses, actions = [], []
    for incident in unhealthy:
        items = grouped.get(incident["workload"], [])
        hypotheses.append({"workload": incident["workload"], "category": incident["category"], **hypothesis_for(incident["category"], items), "citations": incident["citations"]})
        actions.extend(actions_for(kube, store, namespace, incident["workload"], incident["category"], items, revisions))
    cited = {c for incident in unhealthy for c in incident["citations"]}
    timeline = sorted(({"time": e["time"], "description": f"{e['object']} {e['signal']}: {e['summary']}", "evidence": e["id"]} for e in evidence if e.get("time")), key=lambda t: instant(t["time"]))
    cited |= {t["evidence"] for t in timeline}
    names = ", ".join(f"{i['workload']} ({i['category']})" for i in unhealthy)
    summary = f"{len(unhealthy)} of {len(incidents)} workloads in {namespace} are unhealthy: {names}." if unhealthy else f"All {len(incidents)} workloads in {namespace} look healthy."
    dark = [i["workload"] for i in unhealthy if i["category"] in ("image-pull", "no-endpoints", "crashloop-config", "oom")]
    impact = f"{len(dark)} workload(s) cannot serve: {', '.join(dark)}. Requests routed to them fail or time out." if dark else "No user-facing impact detected from cluster state."
    return {
        "namespace": namespace,
        "generated_at": now or utc_now(),
        "summary": summary,
        "impact": impact,
        "timeline": timeline,
        "hypotheses": hypotheses,
        "actions": actions,
        "evidence": [e for e in evidence if e["id"] in cited],
    }


def render_markdown(report: Dict[str, Any]) -> str:
    lines = [f"# Incident report: {report['namespace']}", "", f"Generated {report['generated_at']}. Every claim cites an evidence id from `copilot collect`.", "", "## Summary", "", report["summary"], "", "## Impact", "", report["impact"], "", "## Timeline", ""]
    lines += [f"- {t['time']} {t['description']} (`{t['evidence']}`)" for t in report["timeline"]] or ["- no timestamped evidence"]
    lines += ["", "## Root cause", ""]
    for h in report["hypotheses"]:
        lines.append(f"- **{h['workload']}** `{h['category']}` (confidence {h['confidence']:.2f}): {h['hypothesis']}. Evidence: {', '.join(f'`{c}`' for c in h['citations'])}")
    lines += ["", "## Actions", "", "Every action is a proposal. Nothing has been changed.", "", "| Id | Action | Target | Risk | What |", "|---|---|---|---|---|"]
    lines += [f"| `{a['id']}` | {a['action']} | {a['target']} | {a['risk']} | {a['reason']} |" for a in report["actions"]]
    lines += ["", "## Evidence", ""]
    lines += [f"- `{e['id']}` {e['object']} {e['signal']}: {e['summary']}" for e in report["evidence"]]
    return "\n".join(lines) + "\n"
