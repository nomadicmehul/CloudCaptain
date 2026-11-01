"""planguard: evaluate declarative rules against an OpenTofu or Terraform plan JSON and exit non-zero on deny."""
from __future__ import annotations

import argparse
import json
import operator
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

LEVELS = ("deny", "warn")
CHECKS = ("attribute", "no_delete", "no_replace", "module_source")
PRESENCE_OPS = ("exists", "absent")
COMPARISONS: Dict[str, Callable[[Any, Any], bool]] = {"lt": operator.lt, "lte": operator.le, "gt": operator.gt, "gte": operator.ge}
VALUE_OPS = ("equals", "not_equals", "in", "not_in", "matches", "not_matches", *COMPARISONS)
WRITE_ACTIONS = ("create", "update")
RULE_FIELDS = {"id", "level", "check", "message", "type", "address", "actions", "path", "op", "value", "unknown", "allow"}
SYMBOLS = {"create": "+", "update": "~", "delete": "-", "replace": "-/+", "replace (create before destroy)": "+/-", "read": "<=", "no-op": " "}
MISSING = object()

Runner = Callable[[Sequence[str]], Tuple[int, str, str]]


class PolicyError(Exception):
    pass


class PlanError(Exception):
    pass


@dataclass
class Rule:
    id: str
    level: str
    check: str
    message: str
    types: List[str] = field(default_factory=lambda: ["*"])
    addresses: List[str] = field(default_factory=lambda: ["*"])
    actions: List[str] = field(default_factory=lambda: list(WRITE_ACTIONS))
    path: List[Any] = field(default_factory=list)
    op: str = ""
    value: Any = None
    allow_unknown: bool = False
    allow: List[str] = field(default_factory=list)


@dataclass
class Change:
    address: str
    type: str
    mode: str
    actions: List[str]
    after: Any
    after_unknown: Any
    action_reason: str = ""

    @property
    def action(self) -> str:
        return action_label(self.actions)


@dataclass
class ModuleCall:
    address: str
    source: str
    version: str = ""


@dataclass
class Plan:
    changes: List[Change]
    module_calls: List[ModuleCall]
    tool_version: str = ""


@dataclass
class Violation:
    rule: str
    level: str
    address: str
    action: str
    message: str
    detail: str

    def key(self) -> Tuple[str, str, str]:
        return (self.level, self.rule, self.address)


def action_label(actions: Sequence[str]) -> str:
    acts = list(actions)
    if acts == ["delete", "create"]:
        return "replace"
    if acts == ["create", "delete"]:
        return "replace (create before destroy)"
    return ",".join(acts) or "no-op"


def glob_regex(pattern: str) -> "re.Pattern[str]":
    parts = [".*" if char == "*" else "." if char == "?" else re.escape(char) for char in pattern]
    return re.compile("".join(parts), re.DOTALL)


def glob_match(patterns: Sequence[str], text: str) -> bool:
    return any(glob_regex(pattern).fullmatch(text) for pattern in patterns)


def lookup(document: Any, path: Sequence[Any]) -> Any:
    node = document
    for segment in path:
        if isinstance(node, dict):
            key = str(segment)
            if key not in node:
                return MISSING
            node = node[key]
        elif isinstance(node, list):
            try:
                index = int(segment)
            except (TypeError, ValueError):
                return MISSING
            if not 0 <= index < len(node):
                return MISSING
            node = node[index]
        else:
            return MISSING
    return node


def is_unknown(after_unknown: Any, path: Sequence[Any]) -> bool:
    return any(lookup(after_unknown, path[:length]) is True for length in range(1, len(path) + 1))


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def same(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    return left == right


def holds(op: str, value: Any, expected: Any) -> bool:
    if op == "equals":
        return same(value, expected)
    if op == "not_equals":
        return not same(value, expected)
    if op == "in":
        return any(same(value, item) for item in expected)
    if op == "not_in":
        return not holds("in", value, expected)
    if op == "matches":
        return isinstance(value, str) and re.search(expected, value) is not None
    if op == "not_matches":
        return not holds("matches", value, expected)
    if op in COMPARISONS:
        return is_number(value) and COMPARISONS[op](value, expected)
    raise PolicyError(f"unknown operator {op!r}")


def as_list(raw: Any, rule_id: str, name: str) -> List[str]:
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list) or not items or not all(isinstance(item, str) and item for item in items):
        raise PolicyError(f'rule "{rule_id}": {name} must be a non-empty string or a list of non-empty strings')
    return list(items)


def parse_path(raw: Any, rule_id: str) -> List[Any]:
    segments = raw.split(".") if isinstance(raw, str) else raw
    if not isinstance(segments, list) or not segments:
        raise PolicyError(f'rule "{rule_id}": path must be a dotted string like "tags.owner" or a list of keys')
    for segment in segments:
        if isinstance(segment, bool) or not isinstance(segment, (str, int)) or segment == "":
            raise PolicyError(f'rule "{rule_id}": path {raw!r} has an empty or invalid segment')
    return list(segments)


def compile_regex(pattern: Any, rule_id: str) -> str:
    if not isinstance(pattern, str):
        raise PolicyError(f'rule "{rule_id}": a regular expression must be a string, got {json.dumps(pattern)}')
    try:
        re.compile(pattern)
    except re.error as error:
        raise PolicyError(f'rule "{rule_id}": invalid regular expression {pattern!r}: {error}') from None
    return pattern


def parse_value(rule_id: str, op: str, item: Dict[str, Any]) -> Any:
    if op in PRESENCE_OPS:
        return None
    if "value" not in item:
        raise PolicyError(f'rule "{rule_id}": op "{op}" needs a "value"')
    value = item["value"]
    if op in ("in", "not_in") and not isinstance(value, list):
        raise PolicyError(f'rule "{rule_id}": op "{op}" needs a list value, got {json.dumps(value)}')
    if op in COMPARISONS and not is_number(value):
        raise PolicyError(f'rule "{rule_id}": op "{op}" needs a number value, got {json.dumps(value)}')
    if op in ("matches", "not_matches"):
        compile_regex(value, rule_id)
    return value


def parse_rule(item: Any, index: int) -> Rule:
    if not isinstance(item, dict):
        raise PolicyError(f"rule #{index + 1}: must be an object")
    rule_id = item.get("id")
    if not isinstance(rule_id, str) or not rule_id:
        raise PolicyError(f"rule #{index + 1}: needs a non-empty string id")
    unknown_fields = sorted(set(item) - RULE_FIELDS)
    if unknown_fields:
        raise PolicyError(f'rule "{rule_id}": unknown field(s) {", ".join(unknown_fields)}; allowed: {", ".join(sorted(RULE_FIELDS))}')
    level = item.get("level", "deny")
    if level not in LEVELS:
        raise PolicyError(f'rule "{rule_id}": level "{level}" is not one of {", ".join(LEVELS)}')
    check = item.get("check", "attribute")
    if check not in CHECKS:
        raise PolicyError(f'rule "{rule_id}": check "{check}" is not one of {", ".join(CHECKS)}')
    message = item.get("message")
    if not isinstance(message, str) or not message:
        raise PolicyError(f'rule "{rule_id}": needs a message that tells the author what to change')
    rule = Rule(rule_id, level, check, message)
    if "type" in item:
        rule.types = as_list(item["type"], rule_id, "type")
    if "address" in item:
        rule.addresses = as_list(item["address"], rule_id, "address")
    if check == "attribute":
        if "path" not in item or "op" not in item:
            raise PolicyError(f'rule "{rule_id}": an attribute check needs "path" and "op"')
        rule.path = parse_path(item["path"], rule_id)
        rule.op = item["op"]
        if rule.op not in PRESENCE_OPS + VALUE_OPS:
            raise PolicyError(f'rule "{rule_id}": op "{rule.op}" is not one of {", ".join(PRESENCE_OPS + VALUE_OPS)}')
        rule.value = parse_value(rule_id, rule.op, item)
        if "actions" in item:
            rule.actions = as_list(item["actions"], rule_id, "actions")
            bad = sorted(set(rule.actions) - set(WRITE_ACTIONS))
            if bad:
                raise PolicyError(f'rule "{rule_id}": actions {bad} are not checkable; an attribute check reads the planned values of create and update')
        unknown = item.get("unknown", "deny")
        if unknown not in ("deny", "allow"):
            raise PolicyError(f'rule "{rule_id}": unknown must be "deny" or "allow", got {json.dumps(unknown)}')
        rule.allow_unknown = unknown == "allow"
    elif check == "module_source":
        rule.allow = [compile_regex(pattern, rule_id) for pattern in as_list(item.get("allow"), rule_id, "allow")]
    return rule


def load_rules(data: Any) -> List[Rule]:
    items = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise PolicyError('the rules file must be {"rules": [...]} with at least one rule')
    rules = [parse_rule(item, index) for index, item in enumerate(items)]
    ids = [rule.id for rule in rules]
    duplicates = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
    if duplicates:
        raise PolicyError(f"duplicate rule id(s): {', '.join(duplicates)}")
    return rules


def module_calls(configuration: Any) -> List[ModuleCall]:
    calls: List[ModuleCall] = []

    def visit(module: Any, prefix: str) -> None:
        children = module.get("module_calls") if isinstance(module, dict) else None
        for name in sorted(children or {}):
            call = children[name] or {}
            address = f"{prefix}module.{name}"
            calls.append(ModuleCall(address, str(call.get("source", "")), str(call.get("version_constraint", ""))))
            visit(call.get("module") or {}, address + ".")

    root = configuration.get("root_module") if isinstance(configuration, dict) else None
    visit(root or {}, "")
    return calls


def load_plan(data: Any) -> Plan:
    if not isinstance(data, dict):
        raise PlanError("plan JSON must be an object")
    if data.get("errored"):
        raise PlanError('the plan errored ("errored": true); a policy verdict on a failed plan means nothing, fix the plan first')
    if "planned_values" not in data and "resource_changes" not in data:
        if "values" in data:
            raise PlanError("this is state, not a plan: run `tofu plan -out=plan.out`, then `tofu show -json plan.out`")
        raise PlanError("not a plan JSON: expected resource_changes or planned_values (the output of `tofu show -json PLANFILE`)")
    changes = []
    for item in data.get("resource_changes") or []:
        change = item.get("change") or {}
        changes.append(Change(
            str(item.get("address", "")),
            str(item.get("type", "")),
            str(item.get("mode", "")),
            [str(action) for action in change.get("actions") or []],
            change.get("after"),
            change.get("after_unknown"),
            str(item.get("action_reason", "")),
        ))
    return Plan(changes, module_calls(data.get("configuration") or {}), str(data.get("terraform_version", "")))


def shown_path(path: Sequence[Any]) -> str:
    return ".".join(str(segment) for segment in path)


def attribute_problem(rule: Rule, change: Change) -> Optional[str]:
    where = shown_path(rule.path)
    if is_unknown(change.after_unknown, rule.path):
        return None if rule.allow_unknown else f"{where} is known after apply, so it cannot be checked at plan time"
    value = lookup(change.after, rule.path)
    present = value is not MISSING and value is not None
    if rule.op == "exists":
        return None if present else f"{where} is missing"
    if rule.op == "absent":
        return f"{where} is set to {json.dumps(value)}" if present else None
    if not present or holds(rule.op, value, rule.value):
        return None
    return f"{where} is {json.dumps(value)}, want {rule.op} {json.dumps(rule.value)}"


def selected(rule: Rule, change: Change) -> bool:
    return change.mode == "managed" and glob_match(rule.types, change.type) and glob_match(rule.addresses, change.address)


def change_problem(rule: Rule, change: Change) -> Optional[str]:
    if not selected(rule, change):
        return None
    if rule.check == "attribute":
        if not set(rule.actions) & set(change.actions):
            return None
        return attribute_problem(rule, change)
    reason = f"; reason {change.action_reason}" if change.action_reason else ""
    if rule.check == "no_delete" and "delete" in change.actions:
        return f"the plan would {change.action} a protected address{reason}"
    if rule.check == "no_replace" and "delete" in change.actions and "create" in change.actions:
        return f"the plan would destroy and recreate it{reason}"
    return None


def evaluate(rules: Sequence[Rule], plan: Plan) -> List[Violation]:
    violations: List[Violation] = []
    for rule in rules:
        if rule.check == "module_source":
            for call in plan.module_calls:
                if glob_match(rule.addresses, call.address) and not any(re.search(pattern, call.source) for pattern in rule.allow):
                    violations.append(Violation(rule.id, rule.level, call.address, "config", rule.message, f"source {json.dumps(call.source)} matches none of the allowed patterns"))
            continue
        for change in plan.changes:
            problem = change_problem(rule, change)
            if problem is not None:
                violations.append(Violation(rule.id, rule.level, change.address, change.action, rule.message, problem))
    order = {rule.id: index for index, rule in enumerate(rules)}
    return sorted(violations, key=lambda v: (LEVELS.index(v.level), order[v.rule], v.address))


def counts(violations: Sequence[Violation]) -> Dict[str, int]:
    return {level: sum(1 for v in violations if v.level == level) for level in LEVELS}


def exit_code(violations: Sequence[Violation], fail_on_warn: bool = False) -> int:
    tally = counts(violations)
    return 1 if tally["deny"] or (fail_on_warn and tally["warn"]) else 0


def verdict(violations: Sequence[Violation], fail_on_warn: bool = False) -> str:
    tally = counts(violations)
    blocked = exit_code(violations, fail_on_warn) != 0
    head = f"{tally['deny']} denied, {tally['warn']} warning(s)"
    return f"{head}: apply is blocked." if blocked else f"{head}: policy passed."


def render_text(rules: Sequence[Rule], plan: Plan, violations: Sequence[Violation], fail_on_warn: bool = False) -> str:
    lines = [f"planguard: {len(rules)} rule(s), {len(plan.changes)} resource change(s), {len(plan.module_calls)} module call(s)", ""]
    width = max([len(v.rule) for v in violations] + [4])
    for v in violations:
        symbol = SYMBOLS.get(v.action, "?") if v.action != "config" else "cfg"
        lines.append(f"{v.level.upper():<5} {v.rule:<{width}}  {symbol:<3} {v.address} ({v.action})")
        lines.append(f"      {v.message}: {v.detail}")
    if not violations:
        lines.append("No violations.")
    lines.append("")
    lines.append(verdict(violations, fail_on_warn))
    return "\n".join(lines)


def markdown_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(violations: Sequence[Violation], fail_on_warn: bool = False) -> str:
    lines = [f"### planguard: {verdict(violations, fail_on_warn)}", ""]
    if not violations:
        return "\n".join(lines + ["No violations."])
    lines += ["| Level | Rule | Address | Action | Why |", "|---|---|---|---|---|"]
    for v in violations:
        lines.append(f"| {v.level} | `{v.rule}` | `{markdown_cell(v.address)}` | {v.action} | {markdown_cell(v.message)}: {markdown_cell(v.detail)} |")
    return "\n".join(lines)


def report(rules: Sequence[Rule], plan: Plan, violations: Sequence[Violation], fail_on_warn: bool = False) -> Dict[str, Any]:
    tally = counts(violations)
    return {
        "passed": exit_code(violations, fail_on_warn) == 0,
        "summary": {"deny": tally["deny"], "warn": tally["warn"], "rules": len(rules), "resource_changes": len(plan.changes), "module_calls": len(plan.module_calls)},
        "violations": [asdict(v) for v in violations],
    }


def subprocess_runner(argv: Sequence[str]) -> Tuple[int, str, str]:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: command not found"
    return done.returncode, done.stdout, done.stderr


def read_json(path: str, what: str) -> Any:
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as error:
        raise PlanError(f"cannot read {what} {path}: {error.strerror}") from None
    except json.JSONDecodeError as error:
        raise PlanError(f"{what} {path} is not valid JSON: line {error.lineno} column {error.colno}: {error.msg}") from None


def plan_from_show(planfile: str, tofu: str, runner: Runner) -> Any:
    code, out, err = runner([tofu, "show", "-json", planfile])
    if code != 0:
        raise PlanError(f"`{tofu} show -json {planfile}` failed (exit {code}): {err.strip() or out.strip()}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as error:
        raise PlanError(f"`{tofu} show -json {planfile}` did not print JSON: {error.msg}") from None


def main(argv: Optional[Sequence[str]] = None, runner: Runner = subprocess_runner) -> int:
    parser = argparse.ArgumentParser(prog="planguard", description=__doc__)
    parser.add_argument("plan", nargs="?", help="plan JSON from `tofu show -json PLANFILE` (- for stdin)")
    parser.add_argument("--rules", required=True, help="rules file (JSON)")
    parser.add_argument("--show", metavar="PLANFILE", help="run `tofu show -json PLANFILE` instead of reading a JSON file")
    parser.add_argument("--tofu", default="tofu", help="binary for --show (tofu or terraform)")
    parser.add_argument("--fail-on-warn", action="store_true", help="exit 1 on warnings too")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="machine-readable report")
    output.add_argument("--markdown", action="store_true", help="a table for a job summary or PR comment")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if bool(args.plan) == bool(args.show):
        print("planguard: give exactly one of a plan JSON path or --show PLANFILE", file=sys.stderr)
        return 2
    try:
        rules = load_rules(read_json(args.rules, "rules file"))
        data = plan_from_show(args.show, args.tofu, runner) if args.show else read_json(args.plan, "plan")
        plan = load_plan(data)
    except (PolicyError, PlanError) as error:
        print(f"planguard: {error}", file=sys.stderr)
        return 2
    violations = evaluate(rules, plan)
    if args.json:
        print(json.dumps(report(rules, plan, violations, args.fail_on_warn), indent=2))
    elif args.markdown:
        print(render_markdown(violations, args.fail_on_warn))
    else:
        print(render_text(rules, plan, violations, args.fail_on_warn))
    return exit_code(violations, args.fail_on_warn)


if __name__ == "__main__":
    raise SystemExit(main())
