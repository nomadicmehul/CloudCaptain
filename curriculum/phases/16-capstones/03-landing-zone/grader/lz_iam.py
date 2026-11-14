"""The part of IAM policy evaluation the identity check needs.

Copied in reduced form from the logic of the cloud/iam-policy-evaluator lesson:
an explicit Deny in any policy wins, then an identity-based Allow is required,
then the permissions boundary must also allow. No resource-based policies,
SCPs or session policies: a role's own policies are all moto can return.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

NEGATED = {
    "StringNotEquals": "StringEquals",
    "StringNotEqualsIgnoreCase": "StringEqualsIgnoreCase",
    "StringNotLike": "StringLike",
    "ArnNotEquals": "ArnEquals",
    "ArnNotLike": "ArnLike",
}
POSITIVE = ("StringEquals", "StringEqualsIgnoreCase", "StringLike", "ArnEquals", "ArnLike", "Bool", "Null")


class PolicyError(Exception):
    pass


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def decode_document(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "")
    if text.lstrip().startswith("%7B") or text.lstrip().startswith("%7b"):
        text = urllib.parse.unquote(text)
    try:
        document = json.loads(text)
    except ValueError as error:
        raise PolicyError(f"policy document is not JSON: {error}") from None
    if not isinstance(document, dict):
        raise PolicyError("policy document is not a JSON object")
    return document


def statements(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    body = document.get("Statement")
    return [s for s in as_list(body) if isinstance(s, dict)]


def glob(pattern: str, value: str, ignore_case: bool = False) -> bool:
    regex = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(regex, value, re.DOTALL | (re.IGNORECASE if ignore_case else 0)) is not None


def condition_holds(block: Any, context: Dict[str, List[str]]) -> bool:
    if not block:
        return True
    if not isinstance(block, dict):
        raise PolicyError("Condition must map operators to keys")
    for operator, keys in block.items():
        qualifier, _, base = str(operator).rpartition(":")
        if_exists = base.endswith("IfExists")
        if if_exists:
            base = base[: -len("IfExists")]
        if base not in POSITIVE and base not in NEGATED:
            raise PolicyError(f"condition operator {operator} is not supported by the grader's evaluator")
        for key, wanted in (keys or {}).items():
            values = context.get(str(key).lower())
            expected = [str(w).lower() if isinstance(w, bool) else str(w) for w in as_list(wanted)]
            if base == "Null":
                if not any((w.lower() == "true") == (values is None) for w in expected):
                    return False
                continue
            if values is None:
                if if_exists or qualifier == "ForAllValues":
                    continue
                if base in NEGATED:
                    continue
                return False
            positive = NEGATED.get(base, base)

            def one(value: str) -> bool:
                if positive in ("StringEquals", "ArnEquals"):
                    hit = value in expected
                elif positive == "StringEqualsIgnoreCase":
                    hit = value.lower() in [w.lower() for w in expected]
                elif positive == "Bool":
                    hit = value.lower() in [w.lower() for w in expected]
                else:
                    hit = any(glob(w, value) for w in expected)
                return not hit if base in NEGATED else hit

            if qualifier == "ForAnyValue":
                ok = any(one(v) for v in values)
            elif base in NEGATED or qualifier == "ForAllValues":
                ok = all(one(v) for v in values)
            else:
                ok = any(one(v) for v in values)
            if not ok:
                return False
    return True


def statement_matches(statement: Dict[str, Any], action: str, resource: str, context: Dict[str, List[str]]) -> bool:
    if "NotAction" in statement:
        if any(glob(str(p), action, True) for p in as_list(statement["NotAction"])):
            return False
    elif not any(glob(str(p), action, True) for p in as_list(statement.get("Action"))):
        return False
    if "NotResource" in statement:
        if any(str(p) == "*" or glob(str(p), resource) for p in as_list(statement["NotResource"])):
            return False
    elif not any(str(p) == "*" or glob(str(p), resource) for p in as_list(statement.get("Resource"))):
        return False
    return condition_holds(statement.get("Condition"), context)


@dataclass
class RolePolicies:
    name: str
    identity: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    boundary: Optional[Tuple[str, Dict[str, Any]]] = None


@dataclass
class Decision:
    decision: str
    policy: str = ""
    sid: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == "allowed"


def find(policies: Sequence[Tuple[str, Dict[str, Any]]], effect: str, action: str, resource: str, context: Dict[str, List[str]]) -> Optional[Tuple[str, str]]:
    for name, document in policies:
        for index, statement in enumerate(statements(document)):
            if statement.get("Effect") == effect and statement_matches(statement, action, resource, context):
                return name, str(statement.get("Sid") or f"#{index + 1}")
    return None


def evaluate(role: RolePolicies, action: str, resource: str = "*", context: Optional[Dict[str, Any]] = None) -> Decision:
    ctx = {str(k).lower(): [str(x) for x in as_list(v)] for k, v in (context or {}).items()}
    everything = list(role.identity) + ([role.boundary] if role.boundary else [])
    deny = find(everything, "Deny", action, resource, ctx)
    if deny:
        return Decision("explicitDeny", deny[0], deny[1])
    allow = find(role.identity, "Allow", action, resource, ctx)
    if not allow:
        return Decision("implicitDeny", "identity-based policies")
    if role.boundary is not None and not find([role.boundary], "Allow", action, resource, ctx):
        return Decision("implicitDeny", "permissions boundary " + role.boundary[0])
    return Decision("allowed", allow[0], allow[1])


def broad_allows(policies: Sequence[Tuple[str, Dict[str, Any]]]) -> List[str]:
    found = []
    for name, document in policies:
        for index, statement in enumerate(statements(document)):
            if statement.get("Effect") != "Allow":
                continue
            resources = [str(r) for r in as_list(statement.get("Resource"))]
            every_resource = "*" in resources or "NotResource" in statement
            sid = str(statement.get("Sid") or f"#{index + 1}")
            if every_resource and any(str(a) in ("*", "*:*") for a in as_list(statement.get("Action"))):
                found.append(f'{name} statement "{sid}" allows Action "*" on Resource "*"')
            elif every_resource and "NotAction" in statement:
                found.append(f'{name} statement "{sid}" allows NotAction on Resource "*" (everything except a list)')
    return found
