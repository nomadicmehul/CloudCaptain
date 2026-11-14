from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional

from .runner import PaasError

SUCCEEDED = "succeeded"
FAILED = "failed"
PENDING = "pending"


def config_hash(config: Dict[str, str]) -> str:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def rfc3339(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


class Store:
    def __init__(self, data_dir: str, clock: Callable[[], float] = time.time):
        self.root = os.path.join(os.path.abspath(data_dir), ".tinypaas", "apps")
        self.clock = clock

    def path(self, app: str) -> str:
        return os.path.join(self.root, f"{app}.json")

    def exists(self, app: str) -> bool:
        return os.path.isfile(self.path(app))

    def load(self, app: str) -> Dict[str, Any]:
        if not self.exists(app):
            return {"app": app, "current": None, "formation": {}, "config": {}, "remote": None, "releases": []}
        with open(self.path(app), encoding="utf-8") as f:
            return json.load(f)

    def save(self, state: Dict[str, Any]) -> None:
        os.makedirs(self.root, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".state-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, self.path(state["app"]))

    def add_release(self, state: Dict[str, Any], build: Dict[str, Any], config: Dict[str, str], description: str, rollback_of: Optional[int] = None) -> Dict[str, Any]:
        number = max((r["number"] for r in state["releases"]), default=0) + 1
        release = {
            "number": number,
            "image": build["image"],
            "digest": build.get("digest"),
            "commit": build.get("commit"),
            "manifest": build["manifest"],
            "config": dict(config),
            "config_hash": config_hash(config),
            "time": rfc3339(self.clock()),
            "status": PENDING,
            "description": description,
        }
        if rollback_of is not None:
            release["rollback_of"] = rollback_of
        state["releases"].append(release)
        return release


def find(state: Dict[str, Any], number: int) -> Dict[str, Any]:
    for release in state["releases"]:
        if release["number"] == number:
            return release
    raise PaasError(f"{state['app']} has no release v{number}")


def current(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return find(state, state["current"]) if state.get("current") else None


def require_current(state: Dict[str, Any]) -> Dict[str, Any]:
    release = current(state)
    if release is None:
        raise PaasError(f"{state['app']} has no running release yet: run tinypaas deploy <app-dir> first")
    return release


def rollback_target(state: Dict[str, Any], to: Optional[int]) -> Dict[str, Any]:
    live = require_current(state)
    if to is not None:
        target = find(state, to)
        if target["status"] != SUCCEEDED:
            raise PaasError(f"release v{to} is {target['status']}; only a release that went live can be restored")
        return target
    earlier: List[Dict[str, Any]] = [r for r in state["releases"] if r["number"] < live["number"] and r["status"] == SUCCEEDED]
    if not earlier:
        raise PaasError(f"{state['app']} has no succeeded release before v{live['number']}")
    return earlier[-1]


def public(release: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("number", "image", "digest", "commit", "config_hash", "time", "status", "description", "rollback_of")
    return {k: release[k] for k in keys if k in release}
