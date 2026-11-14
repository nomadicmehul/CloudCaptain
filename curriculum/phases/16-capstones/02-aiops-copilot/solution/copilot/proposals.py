from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .config import Config, Refused
from .evidence import meta
from .kube import Kube, KubeError

ACTIONS = ("restart", "scale", "rollback")
REVISION = "deployment.kubernetes.io/revision"
RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ProposalStore:
    def __init__(self, home: str) -> None:
        self.path = os.path.join(home, "proposals.json")

    def load(self) -> Dict[str, Dict[str, Any]]:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    def save(self, proposals: Dict[str, Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(proposals, handle, indent=2)
        os.replace(tmp, self.path)

    def put(self, proposal: Dict[str, Any]) -> None:
        proposals = self.load()
        proposals[proposal["id"]] = proposal
        self.save(proposals)

    def get(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        return self.load().get(proposal_id)


def flatten(value: Any, prefix: str) -> Dict[str, Any]:
    if isinstance(value, dict) and value:
        flat: Dict[str, Any] = {}
        for key in sorted(value):
            flat.update(flatten(value[key], f"{prefix}.{key}"))
        return flat
    return {prefix: value}


def spec_diff(before: Dict[str, Any], after: Dict[str, Any]) -> List[Dict[str, Any]]:
    old, new = flatten(before.get("spec") or {}, "spec"), flatten(after.get("spec") or {}, "spec")
    return [{"path": path, "before": old.get(path), "after": new.get(path)} for path in sorted(set(old) | set(new)) if old.get(path) != new.get(path)]


def previous_template(kube: Kube, namespace: str, deployment: Dict[str, Any]) -> Dict[str, Any]:
    name = meta(deployment).get("name")
    current = int((meta(deployment).get("annotations") or {}).get(REVISION) or 0)
    candidates = []
    for rs in kube.items("get", "replicasets", "-n", namespace):
        owned = any(ref.get("kind") == "Deployment" and ref.get("name") == name for ref in meta(rs).get("ownerReferences") or [])
        revision = int((meta(rs).get("annotations") or {}).get(REVISION) or 0)
        if owned and 0 < revision < current:
            candidates.append((revision, rs))
    if not candidates:
        raise KubeError(f"deployment/{name} has no earlier revision to roll back to")
    template = json.loads(json.dumps(max(candidates, key=lambda c: c[0])[1]["spec"]["template"]))
    (template.get("metadata") or {}).get("labels", {}).pop("pod-template-hash", None)
    return template


def build_patch(action: str, deployment: Dict[str, Any], replicas: Optional[int], previous: Callable[[], Dict[str, Any]], now: str) -> Dict[str, Any]:
    if action == "restart":
        return {"spec": {"template": {"metadata": {"annotations": {RESTARTED_AT: now}}}}}
    if action == "scale":
        if replicas is None or replicas < 0:
            raise ValueError("scale needs replicas >= 0")
        return {"spec": {"replicas": int(replicas)}}
    if action == "rollback":
        return {"spec": {"template": previous()}}
    raise ValueError(f"unknown action {action!r}; use one of {', '.join(ACTIONS)}")


def risk_of(action: str, current: int, replicas: Optional[int]) -> str:
    if action == "scale":
        if replicas == 0:
            return "high"
        return "medium" if replicas is not None and replicas < current else "low"
    if action == "restart":
        return "low" if current > 1 else "medium"
    return "medium"


def plan(kube: Kube, store: ProposalStore, namespace: str, action: str, target: str, replicas: Optional[int] = None, reason: str = "", now: Callable[[], str] = utc_now) -> Dict[str, Any]:
    target = target.split("/", 1)[-1]
    live = kube.get_json("get", "deployment", target, "-n", namespace)
    patch = build_patch(action, live, replicas, lambda: previous_template(kube, namespace, live), now())
    patch_text = json.dumps(patch, sort_keys=True, separators=(",", ":"))
    code, out, err = kube.run("patch", "deployment", target, "-n", namespace, "--type", "merge", "-p", patch_text, "--dry-run=server", "-o", "json")
    if code != 0:
        raise KubeError(f"server-side dry run refused the change: {(err or out).strip()[:300]}")
    diff = spec_diff(live, json.loads(out))
    digest = hashlib.sha256(f"{namespace}|{action}|{target}|{patch_text}".encode()).hexdigest()[:10]
    current = int((live.get("spec") or {}).get("replicas") or 0)
    proposal = {
        "id": f"p-{digest}",
        "action": action,
        "namespace": namespace,
        "target": target,
        "replicas": replicas,
        "reason": reason,
        "risk": risk_of(action, current, replicas),
        "diff": diff,
        "command": kube.argv("patch", "deployment", target, "-n", namespace, "--type", "merge", "-p", patch_text),
        "status": "proposed",
        "proposed_at": now(),
    }
    store.put(proposal)
    return proposal


def apply(kube: Kube, store: ProposalStore, config: Config, proposal_id: str, now: Callable[[], str] = utc_now) -> Dict[str, Any]:
    proposal = store.get(proposal_id)
    if proposal is None:
        raise Refused(f"no proposal {proposal_id!r} in {store.path}; run copilot ask or copilot report first")
    config.require(proposal["namespace"])
    if proposal.get("status") != "proposed":
        raise Refused(f"proposal {proposal_id} is already {proposal.get('status')}")
    code, out, err = kube.runner(proposal["command"], 60)
    proposal["status"] = "applied" if code == 0 else "failed"
    proposal["applied_at"] = now()
    proposal["result"] = (out or err).strip()[:500]
    store.put(proposal)
    return proposal
