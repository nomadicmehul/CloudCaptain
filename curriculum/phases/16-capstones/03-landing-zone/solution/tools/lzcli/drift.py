from __future__ import annotations

from typing import Any, Dict, List

MISSING = object()


def changed_paths(before: Any, after: Any, prefix: str = "") -> List[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        paths: List[str] = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(changed_paths(before.get(key, MISSING), after.get(key, MISSING), path))
        return paths
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        paths = []
        for index, (left, right) in enumerate(zip(before, after)):
            paths.extend(changed_paths(left, right, f"{prefix}[{index}]"))
        return paths
    if before is MISSING and after is None or after is MISSING and before is None:
        return []
    return [] if before == after else [prefix or "(resource)"]


def drift_report(env: str, plan: Dict[str, Any]) -> Dict[str, Any]:
    resources = []
    for item in plan.get("resource_drift") or []:
        change = item.get("change") or {}
        before = change.get("before")
        after = change.get("after")
        actions = list(change.get("actions") or [])
        if after is None:
            attributes = ["(deleted outside OpenTofu)"]
        else:
            attributes = changed_paths(before or {}, after)
        if not attributes:
            continue
        identity = (after or before or {}).get("id")
        resources.append({
            "address": item.get("address"),
            "type": item.get("type"),
            "id": identity,
            "actions": actions,
            "attributes": attributes,
        })
    resources.sort(key=lambda r: str(r["address"]))
    return {"env": env, "drifted": bool(resources), "resources": resources}
