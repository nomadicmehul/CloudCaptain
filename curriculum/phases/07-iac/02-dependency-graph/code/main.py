"""minigraph: build the dependency graph of an OpenTofu JSON configuration, reject cycles, and plan and apply it with a parallel graph walk."""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

DEFAULT_PARALLELISM = 10
STATE_FILE = "minigraph.tfstate.json"
META_ARGUMENTS = {"depends_on", "lifecycle", "provider", "count", "for_each", "provisioner", "connection", "//"}
UNSUPPORTED_META = {"count", "for_each", "provisioner", "connection", "provider"}
TOP_LEVEL = {"resource", "locals", "output", "terraform", "//"}
UNSUPPORTED_NAMESPACES = {"var", "data", "module", "each", "count", "self", "terraform"}
TRAVERSAL = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*)*$")
DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
ADVERBS = ["boldly", "calmly", "gently", "quickly"]
ADJECTIVES = ["brave", "lucky", "nimble", "quiet", "sunny", "witty"]
ANIMALS = ["gecko", "heron", "lynx", "mole", "otter", "panda"]
HEADLINES = {"create": "will be created", "update": "will be updated in-place", "replace": "must be replaced", "delete": "will be destroyed"}


class ConfigError(ValueError):
    pass


class CycleError(ConfigError):
    def __init__(self, path: Sequence[str]) -> None:
        self.path = list(path)
        super().__init__("Cycle: " + " -> ".join(self.path))


class Unknown:
    def __repr__(self) -> str:
        return "(known after apply)"


UNKNOWN = Unknown()


def is_known(value: Any) -> bool:
    if value is UNKNOWN:
        return False
    if isinstance(value, dict):
        return all(is_known(item) for item in value.values())
    if isinstance(value, list):
        return all(is_known(item) for item in value)
    return True


@dataclass
class Schema:
    arguments: Dict[str, Any]
    required: Set[str]
    force_new: Set[str]
    computed: Set[str]
    stable: Set[str] = field(default_factory=set)

    @property
    def attributes(self) -> Set[str]:
        return set(self.arguments) | self.computed


SCHEMAS: Dict[str, Schema] = {
    "local_file": Schema({"filename": None, "content": ""}, {"filename"}, {"filename", "content"}, {"id"}),
    "random_pet": Schema({"length": 2, "separator": "-", "prefix": None, "keepers": None}, set(), {"length", "separator", "prefix", "keepers"}, {"id"}),
    "terraform_data": Schema({"input": None, "triggers_replace": None}, set(), {"triggers_replace"}, {"id", "output"}, {"id"}),
    "time_sleep": Schema({"create_duration": None, "destroy_duration": None, "triggers": None}, set(), {"triggers"}, {"id"}, {"id"}),
}


@dataclass
class Resource:
    type: str
    name: str
    body: Dict[str, Any]
    depends_on: List[str] = field(default_factory=list)
    create_before_destroy: bool = False

    @property
    def address(self) -> str:
        return f"{self.type}.{self.name}"


@dataclass
class Config:
    resources: Dict[str, Resource] = field(default_factory=dict)
    locals: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Graph:
    deps: Dict[str, Set[str]]
    kinds: Dict[str, str]
    explicit: Set[Tuple[str, str]] = field(default_factory=set)


def template_parts(text: str, where: str) -> List[Tuple[str, str]]:
    parts: List[Tuple[str, str]] = []
    literal: List[str] = []
    position = 0
    while position < len(text):
        if text.startswith("$${", position):
            literal.append("${")
            position += 3
        elif text.startswith("${", position):
            end = text.find("}", position + 2)
            if end == -1:
                raise ConfigError(f"{where}: unterminated ${{ in {text!r}")
            expression = text[position + 2:end].strip()
            if not TRAVERSAL.match(expression):
                raise ConfigError(f"{where}: minigraph understands plain references inside ${{...}}, got ${{{expression}}}")
            if literal:
                parts.append(("text", "".join(literal)))
                literal = []
            parts.append(("ref", expression))
            position = end + 1
        else:
            literal.append(text[position])
            position += 1
    if literal:
        parts.append(("text", "".join(literal)))
    return parts


def references(value: Any, where: str) -> List[str]:
    if isinstance(value, str):
        return [text for kind, text in template_parts(value, where) if kind == "ref"]
    if isinstance(value, list):
        return [ref for index, item in enumerate(value) for ref in references(item, f"{where}[{index}]")]
    if isinstance(value, dict):
        return [ref for key, item in value.items() for ref in references(item, f"{where}.{key}")]
    return []


def config_files(path: str) -> List[str]:
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.tf.json")))
        if not files:
            raise ConfigError(f"{path}: no *.tf.json files found")
        return files
    if not os.path.isfile(path):
        raise ConfigError(f"{path}: no such file or directory")
    return [path]


def parse_resource(rtype: str, name: str, body: Any, source: str) -> Resource:
    where = f"{source}: resource {rtype}.{name}"
    schema = SCHEMAS.get(rtype)
    if schema is None:
        raise ConfigError(f"{source}: resource type {rtype!r} is not implemented (minigraph knows {', '.join(sorted(SCHEMAS))})")
    if not isinstance(body, dict):
        raise ConfigError(f"{where}: the body must be a JSON object")
    unsupported = sorted(set(body) & UNSUPPORTED_META)
    if unsupported:
        raise ConfigError(f"{where}: {unsupported[0]} is not supported by minigraph (count and for_each come in the modules lesson)")
    args = {key: value for key, value in body.items() if key not in META_ARGUMENTS and key != "lifecycle"}
    unexpected = sorted(set(args) - set(schema.arguments))
    if unexpected:
        raise ConfigError(f'{where}: an argument named "{unexpected[0]}" is not expected here')
    missing = sorted(schema.required - set(args))
    if missing:
        raise ConfigError(f'{where}: the argument "{missing[0]}" is required')
    depends_on = body.get("depends_on", [])
    if not isinstance(depends_on, list) or not all(isinstance(item, str) for item in depends_on):
        raise ConfigError(f'{where}: depends_on must be a list of resource addresses such as ["local_file.a"]')
    lifecycle = body.get("lifecycle", {})
    if not isinstance(lifecycle, dict):
        raise ConfigError(f"{where}: lifecycle must be an object")
    extra = sorted(set(lifecycle) - {"create_before_destroy"})
    if extra:
        raise ConfigError(f"{where}: lifecycle.{extra[0]} is not supported by minigraph")
    return Resource(rtype, name, args, list(depends_on), bool(lifecycle.get("create_before_destroy", False)))


def load_config(path: str) -> Config:
    config = Config()
    for file_path in config_files(path):
        source = os.path.basename(file_path)
        try:
            with open(file_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{source}:{exc.lineno}: invalid JSON: {exc.msg}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{source}: the top level must be a JSON object")
        blocks = sorted(set(data) - TOP_LEVEL)
        if blocks:
            raise ConfigError(f"{source}: top-level {blocks[0]!r} blocks are not supported by minigraph")
        for rtype, named in (data.get("resource") or {}).items():
            for name, body in named.items():
                resource = parse_resource(rtype, name, body, source)
                if resource.address in config.resources:
                    raise ConfigError(f"{source}: duplicate resource {resource.address}")
                config.resources[resource.address] = resource
        for name, value in (data.get("locals") or {}).items():
            if name in config.locals:
                raise ConfigError(f"{source}: duplicate local value local.{name}")
            config.locals[name] = value
        for name, body in (data.get("output") or {}).items():
            if not isinstance(body, dict) or "value" not in body:
                raise ConfigError(f'{source}: output "{name}" needs a "value"')
            config.outputs[name] = body["value"]
    return config


def reference_target(expression: str, config: Config, where: str) -> Optional[str]:
    parts = expression.split(".")
    head = parts[0]
    if head == "path":
        if len(parts) != 2 or parts[1] not in ("module", "root", "cwd"):
            raise ConfigError(f"{where}: unknown path attribute {expression}")
        return None
    if head == "local":
        if len(parts) < 2 or parts[1] not in config.locals:
            raise ConfigError(f"{where}: reference to undeclared local value {expression}")
        return f"local.{parts[1]}"
    if head in UNSUPPORTED_NAMESPACES:
        raise ConfigError(f"{where}: {head}.* references are not supported by minigraph")
    if len(parts) < 2:
        raise ConfigError(f"{where}: {expression} is not a reference to a resource")
    address = f"{parts[0]}.{parts[1]}"
    resource = config.resources.get(address)
    if resource is None:
        raise ConfigError(f"{where}: reference to undeclared resource {address}")
    if len(parts) > 2 and parts[2] not in SCHEMAS[resource.type].attributes:
        raise ConfigError(f'{where}: unsupported attribute "{parts[2]}" on {address}')
    return address


def targets_of(value: Any, config: Config, where: str) -> Set[str]:
    found = (reference_target(ref, config, where) for ref in references(value, where))
    return {target for target in found if target}


def config_graph(config: Config) -> Graph:
    deps: Dict[str, Set[str]] = {}
    kinds: Dict[str, str] = {}
    explicit: Set[Tuple[str, str]] = set()
    for address, resource in config.resources.items():
        kinds[address] = "resource"
        targets = targets_of(resource.body, config, address)
        for dep in resource.depends_on:
            if dep not in config.resources:
                raise ConfigError(f"{address}.depends_on: {dep!r} is not a declared resource")
            targets.add(dep)
            explicit.add((address, dep))
        if address in targets:
            raise ConfigError(f"{address}: self-referential block (a resource cannot refer to its own attributes)")
        deps[address] = targets
    for name, value in config.locals.items():
        kinds[f"local.{name}"] = "local"
        deps[f"local.{name}"] = targets_of(value, config, f"local.{name}")
    for name, value in config.outputs.items():
        kinds[f"output.{name}"] = "output"
        deps[f"output.{name}"] = targets_of(value, config, f"output.{name}")
    return Graph(deps, kinds, explicit)


def find_cycle(deps: Dict[str, Set[str]]) -> Optional[List[str]]:
    status: Dict[str, str] = {}
    stack: List[str] = []

    def visit(node: str) -> Optional[List[str]]:
        status[node] = "visiting"
        stack.append(node)
        for dep in sorted(deps.get(node, ())):
            if status.get(dep) == "visiting":
                return stack[stack.index(dep):] + [dep]
            if dep not in status:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        status[node] = "done"
        return None

    for node in sorted(deps):
        if node not in status:
            found = visit(node)
            if found:
                return found
    return None


def waves(deps: Dict[str, Set[str]]) -> List[List[str]]:
    remaining = {node: set(dep for dep in found if dep in deps) for node, found in deps.items()}
    result: List[List[str]] = []
    while remaining:
        ready = sorted(node for node, found in remaining.items() if not found)
        if not ready:
            raise CycleError(find_cycle(remaining) or sorted(remaining))
        result.append(ready)
        for node in ready:
            del remaining[node]
        for found in remaining.values():
            found.difference_update(ready)
    return result


def topological_order(deps: Dict[str, Set[str]]) -> List[str]:
    return [node for wave in waves(deps) for node in wave]


def critical_path(deps: Dict[str, Set[str]]) -> List[str]:
    levels = waves(deps)
    if not levels:
        return []
    depth = {node: index for index, wave in enumerate(levels) for node in wave}
    path = [levels[-1][0]]
    while depth[path[-1]] > 0:
        path.append(sorted(dep for dep in deps[path[-1]] if depth[dep] == depth[path[-1]] - 1)[0])
    return list(reversed(path))


def reachable(deps: Dict[str, Set[str]], start: str) -> Set[str]:
    seen: Set[str] = set()
    stack = list(deps.get(start, ()))
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(deps.get(node, ()))
    return seen


def transitive_reduction(deps: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    reduced: Dict[str, Set[str]] = {}
    for node, direct in deps.items():
        implied: Set[str] = set()
        for dep in direct:
            implied |= reachable(deps, dep)
        reduced[node] = {dep for dep in direct if dep not in implied}
    return reduced


def resource_graph(graph: Graph) -> Dict[str, Set[str]]:
    def resources_behind(node: str, seen: Set[str]) -> Set[str]:
        found: Set[str] = set()
        for dep in graph.deps.get(node, ()):
            if graph.kinds.get(dep) == "resource":
                found.add(dep)
            elif dep not in seen:
                seen.add(dep)
                found |= resources_behind(dep, seen)
        return found

    return {node: resources_behind(node, set()) for node, kind in graph.kinds.items() if kind == "resource"}


def effective_cbd(config: Config, resource_deps: Dict[str, Set[str]]) -> Dict[str, bool]:
    result = {address: False for address in config.resources}
    for address, resource in config.resources.items():
        if resource.create_before_destroy:
            result[address] = True
            for dep in reachable(resource_deps, address):
                result[dep] = True
    return result


def render_dot(deps: Dict[str, Set[str]], kinds: Optional[Dict[str, str]] = None, dashed: Set[Tuple[str, str]] = frozenset(), red: Set[Tuple[str, str]] = frozenset()) -> str:
    kinds = kinds or {}
    lines = ["digraph {", '\tcompound = "true"', '\tnewrank = "true"', '\tsubgraph "root" {']
    for node in sorted(deps):
        shape = "note" if kinds.get(node) in ("local", "output") else "box"
        lines.append(f'\t\t"{node}" [label = "{node}", shape = "{shape}"]')
    for node in sorted(deps):
        for dep in sorted(deps[node]):
            style = []
            if (node, dep) in dashed:
                style.append('style = "dashed"')
            if (node, dep) in red:
                style.append('color = "red"')
            lines.append(f'\t\t"{node}" -> "{dep}"' + (f" [{', '.join(style)}]" if style else ""))
    lines += ["\t}", "}"]
    return "\n".join(lines)


def cycle_edges(path: Sequence[str]) -> Set[Tuple[str, str]]:
    return {(path[index], path[index + 1]) for index in range(len(path) - 1)}


def parse_duration(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    text = str(value)
    pieces = DURATION.findall(text)
    if not pieces or "".join(number + unit for number, unit in pieces) != text:
        raise ConfigError(f"invalid duration {text!r} (use forms like 30s, 500ms, 1m30s)")
    return sum(float(number) * DURATION_UNITS[unit] for number, unit in pieces)


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class Provider:
    def __init__(self, root: str = ".", sleep: Callable[[float], None] = time.sleep, seed: Optional[int] = None) -> None:
        self.root = root
        self.sleep = sleep
        self.random = random.Random(seed)
        self.lock = threading.Lock()

    def path(self, filename: str) -> str:
        return filename if os.path.isabs(filename) else os.path.join(self.root, filename)

    def read(self, rtype: str, attrs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if rtype == "local_file":
            full = self.path(attrs["filename"])
            if not os.path.isfile(full):
                return None
            with open(full, encoding="utf-8") as handle:
                if sha1(handle.read()) != attrs.get("id"):
                    return None
        return dict(attrs)

    def pet_name(self, length: int, separator: str, prefix: Optional[str]) -> str:
        with self.lock:
            words = [self.random.choice(ANIMALS)]
            if length >= 2:
                words.insert(0, self.random.choice(ADJECTIVES))
            words = [self.random.choice(ADVERBS) for _ in range(max(length - 2, 0))] + words
        return separator.join(([prefix] if prefix else []) + words)

    def create(self, rtype: str, args: Dict[str, Any]) -> Dict[str, Any]:
        attrs = dict(args)
        if rtype == "local_file":
            full = self.path(args["filename"])
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(args["content"])
            attrs["id"] = sha1(args["content"])
        elif rtype == "random_pet":
            attrs["id"] = self.pet_name(int(args["length"]), args["separator"], args.get("prefix"))
        elif rtype == "terraform_data":
            with self.lock:
                attrs["id"] = str(uuid.UUID(int=self.random.getrandbits(128), version=4))
            attrs["output"] = args.get("input")
        elif rtype == "time_sleep":
            self.sleep(parse_duration(args.get("create_duration")))
            attrs["id"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return attrs

    def update(self, rtype: str, prior: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        attrs = dict(prior)
        attrs.update(args)
        if rtype == "terraform_data":
            attrs["output"] = args.get("input")
        return attrs

    def delete(self, rtype: str, attrs: Dict[str, Any]) -> None:
        if rtype == "local_file":
            full = self.path(attrs["filename"])
            if os.path.isfile(full):
                os.remove(full)
        elif rtype == "time_sleep":
            self.sleep(parse_duration(attrs.get("destroy_duration")))


def stringify(value: Any, where: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if value is None:
        raise ConfigError(f"{where}: cannot interpolate a null value into a string")
    raise ConfigError(f"{where}: cannot include a list or object in a string template")


class Evaluator:
    def __init__(self, config: Config, values: Dict[str, Dict[str, Any]], cwd: str = ".") -> None:
        self.config = config
        self.values = values
        self.cwd = cwd

    def resolve(self, expression: str, where: str) -> Any:
        parts = expression.split(".")
        if parts[0] == "path":
            return os.path.abspath(self.cwd) if parts[1] == "cwd" else "."
        if parts[0] == "local":
            value = self.evaluate(self.config.locals[parts[1]], f"local.{parts[1]}")
        else:
            value = self.values.get(f"{parts[0]}.{parts[1]}", UNKNOWN)
        for key in parts[2:]:
            if value is UNKNOWN:
                return UNKNOWN
            if not isinstance(value, dict) or key not in value:
                raise ConfigError(f"{where}: {expression} has no attribute {key!r}")
            value = value[key]
        return value

    def evaluate(self, value: Any, where: str) -> Any:
        if isinstance(value, str):
            parts = template_parts(value, where)
            if len(parts) == 1 and parts[0][0] == "ref":
                return self.resolve(parts[0][1], where)
            pieces = []
            for kind, text in parts:
                if kind == "text":
                    pieces.append(text)
                    continue
                resolved = self.resolve(text, where)
                if not is_known(resolved):
                    return UNKNOWN
                pieces.append(stringify(resolved, where))
            return "".join(pieces)
        if isinstance(value, list):
            return [self.evaluate(item, f"{where}[{index}]") for index, item in enumerate(value)]
        if isinstance(value, dict):
            return {key: self.evaluate(item, f"{where}.{key}") for key, item in value.items()}
        return value

    def arguments(self, resource: Resource) -> Dict[str, Any]:
        args = dict(SCHEMAS[resource.type].arguments)
        args.update(self.evaluate(resource.body, resource.address))
        return args


@dataclass
class Change:
    address: str
    type: str
    verb: str
    reason: str = ""
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None
    create_before_destroy: bool = False
    forced_by: List[str] = field(default_factory=list)

    def actions(self) -> List[str]:
        if self.verb == "replace":
            return ["create", "delete"] if self.create_before_destroy else ["delete", "create"]
        return {"create": ["create"], "update": ["update"], "delete": ["delete"], "noop": ["no-op"]}[self.verb]

    def symbol(self) -> str:
        if self.verb == "replace":
            return "+/-" if self.create_before_destroy else "-/+"
        return {"create": "+", "update": "~", "delete": "-", "noop": " "}[self.verb]


def with_unknown_computed(schema: Schema, args: Dict[str, Any]) -> Dict[str, Any]:
    after = dict(args)
    for key in schema.computed:
        after[key] = UNKNOWN
    return after


def plan_resource(resource: Resource, args: Dict[str, Any], prior: Optional[dict], current: Optional[Dict[str, Any]], cbd: bool) -> Change:
    schema = SCHEMAS[resource.type]
    address = resource.address
    if prior is None:
        return Change(address, resource.type, "create", "not in state", None, with_unknown_computed(schema, args), cbd)
    if current is None:
        return Change(address, resource.type, "create", "in state but gone from the real world (drift)", None, with_unknown_computed(schema, args), cbd)
    changed = sorted(key for key in args if not is_known(args[key]) or args[key] != current.get(key))
    if not changed:
        return Change(address, resource.type, "noop", "", current, dict(current), cbd)
    forced = [key for key in changed if key in schema.force_new]
    if forced:
        return Change(address, resource.type, "replace", f"{', '.join(forced)} forces replacement", current, with_unknown_computed(schema, args), cbd, forced)
    after = dict(current)
    after.update(args)
    for key in schema.computed - schema.stable:
        after[key] = UNKNOWN
    return Change(address, resource.type, "update", f"{', '.join(changed)} changed", current, after, cbd)


def empty_state() -> dict:
    return {"version": 1, "serial": 0, "resources": {}, "outputs": {}}


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return empty_state()
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
    state.setdefault("outputs", {})
    return state


def save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")


def state_deps(state: dict) -> Dict[str, Set[str]]:
    known = set(state["resources"])
    return {address: set(entry.get("dependencies", [])) & known for address, entry in state["resources"].items()}


def refresh(state: dict, provider: Provider) -> Dict[str, Optional[Dict[str, Any]]]:
    return {address: provider.read(entry["type"], entry["attrs"]) for address, entry in state["resources"].items()}


def plan(config: Config, graph: Graph, state: dict, actual: Dict[str, Optional[Dict[str, Any]]], destroy: bool = False) -> List[Change]:
    changes: List[Change] = []
    planned: Dict[str, Dict[str, Any]] = {}
    if not destroy:
        resource_deps = resource_graph(graph)
        cbd = effective_cbd(config, resource_deps)
        evaluator = Evaluator(config, planned)
        for address in topological_order(resource_deps):
            resource = config.resources[address]
            change = plan_resource(resource, evaluator.arguments(resource), state["resources"].get(address), actual.get(address), cbd[address])
            planned[address] = change.after or {}
            changes.append(change)
    wanted = set() if destroy else set(config.resources)
    for address in reversed(topological_order(state_deps(state))):
        if address not in wanted:
            entry = state["resources"][address]
            changes.append(Change(address, entry["type"], "delete", "destroy" if destroy else "not in configuration", entry["attrs"], None, bool(entry.get("create_before_destroy"))))
    return changes


def summary(changes: Sequence[Change]) -> Dict[str, int]:
    counts = {"add": 0, "change": 0, "destroy": 0}
    for change in changes:
        if change.verb in ("create", "replace"):
            counts["add"] += 1
        if change.verb in ("delete", "replace"):
            counts["destroy"] += 1
        if change.verb == "update":
            counts["change"] += 1
    return counts


def show_value(value: Any) -> str:
    return "(known after apply)" if not is_known(value) else json.dumps(value)


def render_plan(changes: Sequence[Change]) -> str:
    lines: List[str] = []
    for change in sorted(changes, key=lambda item: item.address):
        if change.verb == "noop":
            continue
        lines.append(f"  # {change.address} {HEADLINES[change.verb]} ({change.reason})")
        lines.append(f"  {change.symbol()} {change.address}")
        before, after = change.before or {}, change.after or {}
        rows: List[Tuple[str, str, str]] = []
        for key in sorted(set(before) | set(after)):
            old, new = before.get(key), after.get(key)
            if change.verb == "create" and new is not None:
                rows.append(("+", key, show_value(new)))
            elif change.verb == "delete" and old is not None:
                rows.append(("-", key, show_value(old)))
            elif change.verb in ("update", "replace") and (new is UNKNOWN or old != new):
                note = "  # forces replacement" if key in change.forced_by else ""
                rows.append(("~", key, f"{show_value(old)} -> {show_value(new)}{note}"))
        width = max((len(key) for _, key, _ in rows), default=0)
        lines += [f"      {sign} {key.ljust(width)} = {text}" for sign, key, text in rows]
        lines.append("")
    if not lines:
        lines = ["No changes. Your infrastructure matches the configuration.", ""]
    counts = summary(changes)
    lines.append(f"Plan: {counts['add']} to add, {counts['change']} to change, {counts['destroy']} to destroy.")
    return "\n".join(lines)


def jsonable(value: Any) -> Any:
    if value is UNKNOWN:
        return None
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    return value


def plan_json(changes: Sequence[Change]) -> dict:
    resource_changes = []
    for change in changes:
        after = change.after
        resource_changes.append({
            "address": change.address,
            "type": change.type,
            "name": change.address.split(".", 1)[1],
            "action_reason": change.reason,
            "create_before_destroy": change.create_before_destroy,
            "change": {
                "actions": change.actions(),
                "before": change.before,
                "after": None if after is None else {key: value for key, value in after.items() if is_known(value)},
                "after_unknown": {} if after is None else {key: True for key, value in after.items() if not is_known(value)},
            },
        })
    return {"resource_changes": resource_changes, "summary": summary(changes)}


def destroy_node(address: str) -> str:
    return f"{address} (destroy)"


def apply_graph(changes: Sequence[Change], resource_deps: Dict[str, Set[str]], state: dict) -> Tuple[Dict[str, Set[str]], Dict[str, Change]]:
    nodes: Dict[str, Set[str]] = {}
    owner: Dict[str, Change] = {}
    for change in changes:
        if change.verb != "delete":
            nodes[change.address] = set()
            owner[change.address] = change
        if change.verb in ("delete", "replace"):
            nodes[destroy_node(change.address)] = set()
            owner[destroy_node(change.address)] = change
    for change in changes:
        address = change.address
        if change.verb != "delete":
            nodes[address] |= {dep for dep in resource_deps.get(address, ()) if dep in nodes}
        if change.verb == "replace" and change.create_before_destroy:
            nodes[destroy_node(address)].add(address)
            nodes[destroy_node(address)] |= {dependent for dependent, found in resource_deps.items() if address in found and dependent in nodes}
        elif change.verb == "replace":
            nodes[address].add(destroy_node(address))
    for address, found in state_deps(state).items():
        if destroy_node(address) in nodes:
            for dep in found:
                if destroy_node(dep) in nodes:
                    nodes[destroy_node(dep)].add(destroy_node(address))
    return nodes, owner


@dataclass
class WalkResult:
    completed: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    skipped: List[str] = field(default_factory=list)
    events: List[Tuple[float, str, str]] = field(default_factory=list)
    max_in_flight: int = 0


def walk(deps: Dict[str, Set[str]], visit: Callable[[str], None], parallelism: int = DEFAULT_PARALLELISM, clock: Callable[[], float] = time.monotonic) -> WalkResult:
    if parallelism < 1:
        raise ValueError("parallelism must be at least 1")
    cycle = find_cycle(deps)
    if cycle:
        raise CycleError(cycle)
    waiting = {node: set(dep for dep in found if dep in deps) for node, found in deps.items()}
    dependents: Dict[str, Set[str]] = {node: set() for node in deps}
    for node, found in waiting.items():
        for dep in found:
            dependents[dep].add(node)
    result = WalkResult()
    lock = threading.Lock()
    in_flight = [0]
    origin = clock()

    def run(node: str) -> None:
        with lock:
            in_flight[0] += 1
            result.max_in_flight = max(result.max_in_flight, in_flight[0])
            result.events.append((round(clock() - origin, 4), node, "start"))
        try:
            visit(node)
        finally:
            with lock:
                in_flight[0] -= 1
                result.events.append((round(clock() - origin, 4), node, "finish"))

    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        running: Dict[Any, str] = {}
        ready = sorted(node for node, found in waiting.items() if not found)
        while ready or running:
            for node in ready:
                running[pool.submit(run, node)] = node
            ready = []
            done, _ = wait(list(running), return_when=FIRST_COMPLETED)
            for future in sorted(done, key=lambda item: running[item]):
                node = running.pop(future)
                error = future.exception()
                if error is not None:
                    result.failed[node] = str(error)
                    continue
                result.completed.append(node)
                for child in sorted(dependents[node]):
                    waiting[child].discard(node)
                    if not waiting[child]:
                        ready.append(child)
    finished = set(result.completed) | set(result.failed)
    result.skipped = sorted(node for node in deps if node not in finished)
    return result


@dataclass
class ApplyResult:
    state: dict
    walk: WalkResult
    lines: List[str]
    counts: Dict[str, int]
    outputs: Dict[str, Any]


def elapsed(seconds: float) -> str:
    return f"{int(round(seconds))}s"


def deposed_key(change: Change) -> str:
    return sha1(f"{change.address}/{(change.before or {}).get('id')}")[:8]


def apply_changes(config: Config, graph: Graph, changes: Sequence[Change], state: dict, provider: Provider, parallelism: int = DEFAULT_PARALLELISM, emit: Optional[Callable[[str], None]] = None, clock: Callable[[], float] = time.monotonic) -> ApplyResult:
    resource_deps = resource_graph(graph)
    nodes, owner = apply_graph(changes, resource_deps, state)
    lock = threading.Lock()
    resources = {address: dict(entry) for address, entry in state["resources"].items()}
    values = {change.address: dict(change.after or {}) for change in changes if change.verb == "noop"}
    for address in values:
        resources[address] = {**resources[address], "attrs": values[address], "dependencies": sorted(resource_deps.get(address, ()))}
    evaluator = Evaluator(config, values)
    lines: List[str] = []
    counts = {"added": 0, "changed": 0, "destroyed": 0}

    def say(line: str) -> None:
        with lock:
            lines.append(line)
            if emit:
                emit(line)

    def visit(node: str) -> None:
        change = owner[node]
        address = change.address
        started = clock()
        if node == destroy_node(address):
            label = f"{address} (deposed object {deposed_key(change)})" if change.verb == "replace" and change.create_before_destroy else address
            before = change.before or {}
            say(f"{label}: Destroying... [id={before.get('id')}]")
            provider.delete(change.type, before)
            with lock:
                if not (change.verb == "replace" and change.create_before_destroy):
                    resources.pop(address, None)
                counts["destroyed"] += 1
            say(f"{label}: Destruction complete after {elapsed(clock() - started)}")
            return
        if change.verb == "noop":
            return
        args = evaluator.arguments(config.resources[address])
        if not is_known(args):
            raise RuntimeError(f"{address}: a value is still unknown at apply time")
        if change.verb == "update":
            say(f"{address}: Modifying... [id={(change.before or {}).get('id')}]")
            attrs = provider.update(change.type, change.before or {}, args)
            done = "Modifications complete"
        else:
            say(f"{address}: Creating...")
            attrs = provider.create(change.type, args)
            done = "Creation complete"
        with lock:
            values[address] = attrs
            resources[address] = {"type": change.type, "attrs": attrs, "dependencies": sorted(resource_deps.get(address, ())), "create_before_destroy": change.create_before_destroy}
            counts["changed" if change.verb == "update" else "added"] += 1
        say(f"{address}: {done} after {elapsed(clock() - started)} [id={attrs.get('id')}]")

    result = walk(nodes, visit, parallelism, clock)
    outputs: Dict[str, Any] = {}
    for name, value in config.outputs.items():
        try:
            resolved = evaluator.evaluate(value, f"output.{name}")
        except ConfigError:
            continue
        if is_known(resolved):
            outputs[name] = resolved
    touched = any(node == destroy_node(owner[node].address) or owner[node].verb != "noop" for node in result.completed)
    new_state = {"version": 1, "serial": state["serial"] + (1 if touched else 0), "resources": resources, "outputs": outputs}
    return ApplyResult(new_state, result, lines, counts, outputs)


def render_waves(deps: Dict[str, Set[str]]) -> str:
    levels = waves(deps)
    lines = [f"wave {index}: {', '.join(wave)}" for index, wave in enumerate(levels, start=1)]
    path = critical_path(deps)
    if path:
        lines.append(f"critical path ({len(path)} steps): {' -> '.join(path)}")
    return "\n".join(lines)


def build(root: str) -> Tuple[Config, Graph]:
    config = load_config(root)
    graph = config_graph(config)
    return config, graph


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="minigraph", description=__doc__)
    parser.add_argument("-chdir", "--chdir", default=".", help="directory with *.tf.json; relative filenames are written here")
    parser.add_argument("--state", help=f"state file (default: <chdir>/{STATE_FILE})")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--seed", type=int, help="seed for random_pet names and terraform_data ids")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="load the configuration, build the graph, reject cycles")
    graph_cmd = sub.add_parser("graph", help="print the graph in DOT, like tofu graph")
    graph_cmd.add_argument("-type", "--type", choices=["plan", "plan-destroy", "apply"], default="plan")
    graph_cmd.add_argument("--full", action="store_true", help="keep edges that other paths already imply")
    graph_cmd.add_argument("-draw-cycles", "--draw-cycles", action="store_true", help="print a cyclic graph with the cycle in red")
    sub.add_parser("order", help="print the resource graph as waves and the critical path")
    plan_cmd = sub.add_parser("plan", help="refresh, then print the changes")
    plan_cmd.add_argument("-destroy", "--destroy", action="store_true")
    for name in ("apply", "destroy"):
        command = sub.add_parser(name, help=f"plan and {name} with a parallel walk")
        command.add_argument("-parallelism", "--parallelism", type=int, default=DEFAULT_PARALLELISM)
        command.add_argument("-auto-approve", "--auto-approve", action="store_true", help="accepted for parity with tofu; minigraph never prompts")
    sub.add_parser("show", help="print the state file")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command in ("apply", "destroy") and args.parallelism < 1:
        parser.error("-parallelism must be at least 1")

    state_path = args.state or os.path.join(args.chdir, STATE_FILE)
    graph: Optional[Graph] = None
    try:
        if args.command == "show":
            print(json.dumps(load_state(state_path), indent=2, sort_keys=True))
            return 0
        config, graph = build(args.chdir)
        cycle = find_cycle(graph.deps)
        if cycle and args.command == "graph" and args.draw_cycles:
            print(render_dot(graph.deps, graph.kinds, graph.explicit, cycle_edges(cycle)))
            print(f"minigraph: Error: {CycleError(cycle)}", file=sys.stderr)
            return 1
        if cycle:
            raise CycleError(cycle)
        if args.command == "validate":
            edges = sum(len(found) for found in graph.deps.values())
            print(json.dumps({"valid": True, "nodes": len(graph.deps), "edges": edges}) if args.json else "Success! The configuration is valid.")
            return 0
        if args.command == "order":
            deps = resource_graph(graph)
            print(json.dumps({"waves": waves(deps), "critical_path": critical_path(deps)}, indent=2) if args.json else render_waves(deps))
            return 0
        provider = Provider(args.chdir, seed=args.seed)
        state = load_state(state_path)
        destroy = args.command == "destroy" or (args.command == "plan" and args.destroy) or (args.command == "graph" and args.type == "plan-destroy")
        if args.command == "graph":
            if args.type == "plan":
                deps, kinds, dashed = graph.deps, graph.kinds, graph.explicit
            else:
                changes = plan(config, graph, state, refresh(state, provider), destroy=destroy)
                deps, _ = apply_graph(changes, resource_graph(graph), state)
                kinds, dashed = {}, set()
            if not args.full:
                deps = transitive_reduction(deps)
            if args.json:
                print(json.dumps({"nodes": sorted(deps), "edges": sorted([node, dep] for node, found in deps.items() for dep in found)}, indent=2))
            else:
                print(render_dot(deps, kinds, dashed))
            return 0
        changes = plan(config, graph, state, refresh(state, provider), destroy=destroy)
        if args.command == "plan":
            print(json.dumps(plan_json(changes), indent=2) if args.json else render_plan(changes))
            return 0
        if not args.json:
            print(render_plan(changes))
            print("")
        emit = None if args.json else (lambda line: print(line, flush=True))
        result = apply_changes(config, graph, changes, state, provider, args.parallelism, emit)
        save_state(state_path, result.state)
    except ConfigError as exc:
        print(f"minigraph: Error: {exc}", file=sys.stderr)
        return 1

    failed = result.walk.failed
    if args.json:
        print(json.dumps({
            "plan": plan_json(changes),
            "lines": result.lines,
            "events": [{"t": t, "node": node, "event": event} for t, node, event in result.walk.events],
            "max_in_flight": result.walk.max_in_flight,
            "failed": failed,
            "skipped": result.walk.skipped,
            "counts": result.counts,
            "outputs": jsonable(result.outputs),
        }, indent=2))
    else:
        for node, message in sorted(failed.items()):
            print(f"Error: {node}: {message}", file=sys.stderr)
        if result.walk.skipped:
            print(f"Skipped because a dependency failed: {', '.join(result.walk.skipped)}", file=sys.stderr)
        verb = "Destroy" if args.command == "destroy" else "Apply"
        if args.command == "destroy":
            print(f"\n{verb} complete! Resources: {result.counts['destroyed']} destroyed." if not failed else "\nDestroy failed.")
        else:
            counts = result.counts
            print(f"\n{verb} complete! Resources: {counts['added']} added, {counts['changed']} changed, {counts['destroyed']} destroyed." if not failed else "\nApply failed.")
        if result.outputs and args.command == "apply":
            print("\nOutputs:\n")
            for name, value in sorted(result.outputs.items()):
                print(f"{name} = {json.dumps(value)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
