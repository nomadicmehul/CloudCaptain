"""minitf: desired state in, plan out, apply, state file. An infrastructure-as-code engine over local files."""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set

IMMUTABLE: Dict[str, Set[str]] = {"local_file": {"path"}, "local_dir": {"path"}}
SYMBOLS = {"create": "+", "update": "~", "replace": "-/+", "delete": "-", "noop": " "}


@dataclass
class Resource:
    type: str
    name: str
    attrs: Dict[str, str]

    @property
    def address(self) -> str:
        return f"{self.type}.{self.name}"


@dataclass
class Action:
    verb: str
    address: str
    rtype: str
    reason: str = ""
    before: Optional[Dict[str, str]] = None
    after: Optional[Dict[str, str]] = None


class LocalProvider:
    def __init__(self, root: str) -> None:
        self.root = root

    def _full(self, attrs: Dict[str, str]) -> str:
        return os.path.join(self.root, attrs["path"])

    def read(self, rtype: str, attrs: Dict[str, str]) -> Optional[Dict[str, str]]:
        full = self._full(attrs)
        if rtype == "local_file":
            if not os.path.isfile(full):
                return None
            with open(full, encoding="utf-8") as handle:
                return {"path": attrs["path"], "content": handle.read()}
        if rtype == "local_dir":
            return {"path": attrs["path"]} if os.path.isdir(full) else None
        raise ValueError(f"unknown resource type {rtype!r}")

    def create(self, rtype: str, attrs: Dict[str, str]) -> str:
        full = self._full(attrs)
        if rtype == "local_file":
            os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(attrs.get("content", ""))
        elif rtype == "local_dir":
            os.makedirs(full, exist_ok=True)
        else:
            raise ValueError(f"unknown resource type {rtype!r}")
        return attrs["path"]

    def update(self, rtype: str, attrs: Dict[str, str]) -> str:
        return self.create(rtype, attrs)

    def delete(self, rtype: str, attrs: Dict[str, str]) -> None:
        full = self._full(attrs)
        if rtype == "local_file" and os.path.isfile(full):
            os.remove(full)
        elif rtype == "local_dir" and os.path.isdir(full):
            os.rmdir(full)


def load_config(path: str) -> List[Resource]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    resources = []
    for item in data.get("resources", []):
        attrs = {key: str(value) for key, value in item.items() if key not in ("type", "name")}
        if item["type"] == "local_file":
            attrs.setdefault("content", "")
        resources.append(Resource(item["type"], item["name"], attrs))
    addresses = [resource.address for resource in resources]
    duplicates = sorted({address for address in addresses if addresses.count(address) > 1})
    if duplicates:
        raise ValueError(f"duplicate resource address: {duplicates}")
    return resources


def empty_state() -> dict:
    return {"serial": 0, "resources": {}}


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return empty_state()
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")


def refresh(state: dict, provider: LocalProvider) -> Dict[str, Optional[Dict[str, str]]]:
    return {address: provider.read(entry["type"], entry["attrs"]) for address, entry in state["resources"].items()}


def plan(desired: Sequence[Resource], state: dict, actual: Dict[str, Optional[Dict[str, str]]]) -> List[Action]:
    actions: List[Action] = []
    for resource in desired:
        prior = state["resources"].get(resource.address)
        if prior is None:
            actions.append(Action("create", resource.address, resource.type, "not in state", None, resource.attrs))
            continue
        current = actual.get(resource.address)
        if current is None:
            actions.append(Action("create", resource.address, resource.type, "in state but missing on disk (drift)", None, resource.attrs))
            continue
        changed = sorted(key for key in set(current) | set(resource.attrs) if current.get(key) != resource.attrs.get(key))
        if not changed:
            actions.append(Action("noop", resource.address, resource.type, "", current, current))
        elif set(changed) & IMMUTABLE.get(resource.type, set()):
            actions.append(Action("replace", resource.address, resource.type, f"{', '.join(changed)} changed; forces replacement", current, resource.attrs))
        else:
            actions.append(Action("update", resource.address, resource.type, f"{', '.join(changed)} changed", current, resource.attrs))
    wanted = {resource.address for resource in desired}
    for address, entry in reversed(list(state["resources"].items())):
        if address not in wanted:
            actions.append(Action("delete", address, entry["type"], "removed from config", entry["attrs"], None))
    return actions


def summary(actions: Sequence[Action]) -> Dict[str, int]:
    counts = {"add": 0, "change": 0, "destroy": 0}
    for action in actions:
        if action.verb == "create":
            counts["add"] += 1
        elif action.verb == "update":
            counts["change"] += 1
        elif action.verb == "delete":
            counts["destroy"] += 1
        elif action.verb == "replace":
            counts["add"] += 1
            counts["destroy"] += 1
    return counts


def render_plan(actions: Sequence[Action]) -> str:
    lines: List[str] = []
    for action in actions:
        if action.verb == "noop":
            continue
        lines.append(f"  {SYMBOLS[action.verb]} {action.address}  ({action.reason})")
        for key in sorted(set(action.before or {}) | set(action.after or {})):
            before = (action.before or {}).get(key)
            after = (action.after or {}).get(key)
            if before != after:
                lines.append(f"      {key}: {json.dumps(before)} -> {json.dumps(after)}")
    if not lines:
        lines.append("No changes. Your infrastructure matches the configuration.")
    counts = summary(actions)
    lines.append("")
    lines.append(f"Plan: {counts['add']} to add, {counts['change']} to change, {counts['destroy']} to destroy.")
    return "\n".join(lines)


def apply(actions: Sequence[Action], provider: LocalProvider, state: dict) -> dict:
    new_state = {"serial": state["serial"], "resources": dict(state["resources"])}
    changed = False
    for action in actions:
        if action.verb == "noop":
            continue
        changed = True
        if action.verb in ("delete", "replace"):
            provider.delete(action.rtype, action.before or {})
            new_state["resources"].pop(action.address, None)
        if action.verb in ("create", "replace"):
            resource_id = provider.create(action.rtype, action.after or {})
            new_state["resources"][action.address] = {"type": action.rtype, "id": resource_id, "attrs": dict(action.after or {})}
        elif action.verb == "update":
            resource_id = provider.update(action.rtype, action.after or {})
            new_state["resources"][action.address] = {"type": action.rtype, "id": resource_id, "attrs": dict(action.after or {})}
    if changed:
        new_state["serial"] += 1
    return new_state


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="minitf", description=__doc__)
    parser.add_argument("command", choices=["plan", "apply", "destroy", "refresh", "show"])
    parser.add_argument("--config", default="main.json", help="desired state (JSON)")
    parser.add_argument("--state", default="state.json", help="state file (JSON)")
    parser.add_argument("--root", default=".", help="directory the local provider manages")
    parser.add_argument("--json", action="store_true", help="machine-readable plan")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    provider = LocalProvider(args.root)
    state = load_state(args.state)
    actual = refresh(state, provider)

    if args.command == "show":
        print(json.dumps(state, indent=2))
        return 0
    if args.command == "refresh":
        refreshed = {"serial": state["serial"], "resources": {}}
        for address, entry in state["resources"].items():
            if actual.get(address) is not None:
                refreshed["resources"][address] = {**entry, "attrs": actual[address]}
        if refreshed["resources"] != state["resources"]:
            refreshed["serial"] += 1
        save_state(args.state, refreshed)
        print(f"refreshed: {len(refreshed['resources'])} resource(s) in state (serial {refreshed['serial']})")
        return 0

    desired = [] if args.command == "destroy" else load_config(args.config)
    actions = plan(desired, state, actual)
    if args.json:
        print(json.dumps({"actions": [action.__dict__ for action in actions], "summary": summary(actions)}, indent=2))
    else:
        print(render_plan(actions))
    if args.command in ("apply", "destroy"):
        new_state = apply(actions, provider, state)
        save_state(args.state, new_state)
        counts = summary(actions)
        print(f"\nApply complete. Resources: {counts['add']} added, {counts['change']} changed, {counts['destroy']} destroyed. State serial {new_state['serial']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
