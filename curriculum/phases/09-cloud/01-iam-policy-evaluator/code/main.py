"""iameval: decide an AWS API request the way IAM does (explicit deny, then RCPs and SCPs, the resource-based policy, identity-based policies, the permissions boundary and session policies), name the statement that decided it, and print the AccessDenied message AWS would return."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

DUMP_COMMAND = "aws iam get-account-authorization-details > account.json"
VARIABLES_VERSION = "2012-10-17"
KNOWN_VERSIONS = ("2012-10-17", "2008-10-17")
ASSUME_ACTIONS = ("sts:assumerole", "sts:assumerolewithsaml", "sts:assumerolewithwebidentity")
KIND_LABELS = {
    "identity": "identity-based policy",
    "resource": "resource-based policy",
    "trust": "role trust policy",
    "boundary": "permissions boundary",
    "session": "session policy",
    "scp": "service control policy",
    "rcp": "resource control policy",
}
KIND_ARTICLES = {"identity": "an", "trust": "the"}
DENY_ORDER = ("scp", "rcp", "resource", "trust", "identity", "boundary", "session")
STEP_NAMES = ("explicit deny", "resource control policies", "service control policies", "resource-based policy", "identity-based policies", "permissions boundary", "session policies")
DIRECT_GRANTS = ("wildcard", "user", "session", "service")
GRANT_RANK = {"account": 1, "role": 2, "user": 3, "session": 3, "service": 3, "wildcard": 3}
NEGATED = {
    "StringNotEquals": "StringEquals",
    "StringNotEqualsIgnoreCase": "StringEqualsIgnoreCase",
    "StringNotLike": "StringLike",
    "NumericNotEquals": "NumericEquals",
    "DateNotEquals": "DateEquals",
    "NotIpAddress": "IpAddress",
    "ArnNotEquals": "ArnEquals",
    "ArnNotLike": "ArnLike",
}
POSITIVE = (
    "StringEquals", "StringEqualsIgnoreCase", "StringLike",
    "NumericEquals", "NumericLessThan", "NumericLessThanEquals", "NumericGreaterThan", "NumericGreaterThanEquals",
    "DateEquals", "DateLessThan", "DateLessThanEquals", "DateGreaterThan", "DateGreaterThanEquals",
    "Bool", "BinaryEquals", "IpAddress", "ArnEquals", "ArnLike", "Null",
)
COMPARE: Dict[str, Callable[[float, float], bool]] = {
    "Equals": lambda a, b: a == b,
    "LessThan": lambda a, b: a < b,
    "LessThanEquals": lambda a, b: a <= b,
    "GreaterThan": lambda a, b: a > b,
    "GreaterThanEquals": lambda a, b: a >= b,
}
SPECIAL_VARIABLES = {"*": "*", "?": "?", "$": "$"}
MULTIVALUED_KEYS = ("aws:tagkeys", "aws:principalorgpaths", "aws:resourceorgpaths", "aws:principalservicenameslist", "aws:calledvia")
SINGLE_VALUED_PREFIXES = ("aws:principaltag/", "aws:requesttag/", "aws:resourcetag/")
SINGLE_VALUED_KEYS = (
    "aws:sourceip", "aws:securetransport", "aws:username", "aws:userid", "aws:principalarn", "aws:principalaccount",
    "aws:principaltype", "aws:principalorgid", "aws:currenttime", "aws:epochtime", "aws:sourcevpce", "aws:sourcevpc",
    "aws:multifactorauthpresent", "aws:requestedregion", "aws:resourceaccount", "aws:sourcearn", "aws:sourceaccount",
)
PRINCIPAL_ARN = re.compile(r"^arn:(?P<partition>[\w-]+):(?P<service>iam|sts)::(?P<account>\d{12}):(?P<resource>.+)$")
ACCOUNT_ID = re.compile(r"^\d{12}$")
UNIQUE_ID = re.compile(r"^A(IDA|ROA|GPA)[A-Z0-9]{12,}$")
DENIED = re.compile(r"User: (?P<principal>\S+) is not authorized to perform: (?P<action>[^\s]+?)(?: on resource: \"?(?P<resource>[^\s\"]+)\"?)?(?: (?P<reason>(?:because|with) .*))?$")
EXPLICIT = re.compile(r"^with an explicit deny in (?:an?|the) (?P<label>.+?)(?:: (?P<arn>arn:\S+))?$")
IMPLICIT = re.compile(r"^because no (?P<label>.+?) allows the (?P<action>\S+) action$")
EXTRA_LABELS = {"VPC endpoint policy": "vpce"}
NEXT_STEPS = {
    "identity": "Add an Allow for the action and resource to the principal's identity-based policies (for a role session, the role's policies; for a user, also check group policies), or look for the Deny there. Conditions count: a statement whose condition fails does not apply.",
    "resource": "Read the resource's policy (bucket, key, queue, topic, secret). Cross-account, it must allow this principal and the caller's own identity-based policy must allow the action too.",
    "trust": "Read the role's trust policy (aws iam get-role --query Role.AssumeRolePolicyDocument): it must name this principal (or its account) for this sts action, and its conditions (sts:ExternalId, aws:PrincipalOrgID, MFA) must hold.",
    "boundary": "The principal has a permissions boundary that does not allow the action: aws iam get-user or get-role shows PermissionsBoundary. Identity-based policies cannot grant more than the boundary.",
    "session": "The credentials came from AssumeRole or GetFederationToken with a session policy (--policy or --policy-arns). Get new credentials with a session policy that allows the action, or none.",
    "scp": "A service control policy of the organization (root, an OU or the account) blocks it. Only the organization's administrators can change that; SCPs apply to every principal in member accounts, root users included.",
    "rcp": "A resource control policy of the organization that owns the resource blocks it. Ask the organization's administrators; RCPs apply whoever the caller is.",
    "vpce": "The request went through a VPC endpoint whose endpoint policy does not allow it. Check the endpoint policy of the interface or gateway endpoint the request used.",
    "": "The service did not say which policy type denied the request. Collect every policy type that can apply and evaluate them with iameval.",
}


class PolicyError(Exception):
    pass


def context_text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def as_list(value: object) -> List[object]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


class Context:
    def __init__(self, values: Optional[Dict[str, object]] = None) -> None:
        self._values: Dict[str, List[str]] = {}
        self._names: Dict[str, str] = {}
        self.update(values or {})

    def set(self, key: str, value: object) -> None:
        lower = key.lower()
        if value is None:
            self._values.pop(lower, None)
            self._names.pop(lower, None)
            return
        self._names[lower] = key
        self._values[lower] = [context_text(item) for item in as_list(value)]

    def update(self, values: Dict[str, object]) -> None:
        for key, value in values.items():
            self.set(key, value)

    def get(self, key: str) -> Optional[List[str]]:
        return self._values.get(key.lower())

    def to_json(self) -> Dict[str, object]:
        return {self._names[k]: (v[0] if len(v) == 1 else v) for k, v in sorted(self._values.items())}


EMPTY = Context()


def resolve_variable(body: str, context: Context) -> Optional[str]:
    name, _, default = body.partition(",")
    name = name.strip()
    if name in SPECIAL_VARIABLES:
        return SPECIAL_VARIABLES[name]
    values = context.get(name)
    if values and name.lower() not in MULTIVALUED_KEYS:
        return values[0]
    default = default.strip()
    if len(default) >= 2 and default[0] == default[-1] == "'":
        return default[1:-1]
    return None


def tokens(text: str, context: Context, variables: bool) -> Optional[List[Tuple[str, bool]]]:
    pieces: List[Tuple[str, bool]] = []
    index = 0
    while index < len(text):
        end = text.find("}", index) if variables and text.startswith("${", index) else -1
        if end > 0:
            value = resolve_variable(text[index + 2:end], context)
            if value is None:
                return None
            pieces.append((value, True))
            index = end + 1
        else:
            pieces.append((text[index], False))
            index += 1
    return pieces


def compile_glob(pattern: str, context: Context, variables: bool, arn: bool = False, ignore_case: bool = False) -> Optional["re.Pattern[str]"]:
    pieces = tokens(pattern, context, variables)
    if pieces is None:
        return None
    regex: List[str] = []
    for position, (text, literal) in enumerate(pieces):
        if literal:
            regex.append(re.escape(text))
        elif text == "*":
            ends_segment = position + 1 == len(pieces) or pieces[position + 1] == (":", False)
            regex.append(".*" if not arn or ends_segment else "[^:]*")
        elif text == "?":
            regex.append("[^:]" if arn else ".")
        else:
            regex.append(re.escape(text))
    return re.compile("".join(regex), re.DOTALL | (re.IGNORECASE if ignore_case else 0))


def glob_matches(pattern: str, value: str, context: Context = EMPTY, variables: bool = False, arn: bool = False, ignore_case: bool = False) -> bool:
    regex = compile_glob(pattern, context, variables, arn, ignore_case)
    return regex is not None and regex.fullmatch(value) is not None


def expand_text(text: str, context: Context, variables: bool) -> Optional[str]:
    pieces = tokens(text, context, variables)
    return None if pieces is None else "".join(piece for piece, _ in pieces)


def action_matches(pattern: object, action: str) -> bool:
    return glob_matches(str(pattern), action, ignore_case=True)


def resource_matches(pattern: object, resource: str, context: Context, variables: bool) -> bool:
    return str(pattern) == "*" or glob_matches(str(pattern), resource, context, variables, arn=True)


def arn_matches(value: str, pattern: str, context: Context, variables: bool) -> bool:
    wanted, actual = pattern.split(":", 5), value.split(":", 5)
    if len(wanted) != 6 or len(actual) != 6:
        return glob_matches(pattern, value, context, variables)
    return all(glob_matches(w, a, context, variables) for w, a in zip(wanted, actual))


def parse_number(text: str) -> Optional[float]:
    try:
        return float(text)
    except ValueError:
        return None


def parse_time(text: str) -> Optional[datetime]:
    raw = text.strip()
    try:
        if re.fullmatch(r"-?\d+(\.\d+)?", raw):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        if raw[-1:] in ("Z", "z"):
            raw = raw[:-1] + "+00:00"
        moment = datetime.fromisoformat(raw)
    except (ValueError, OverflowError, OSError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def ip_in(value: str, wanted: str) -> bool:
    try:
        address = ipaddress.ip_address(value.strip())
        network = ipaddress.ip_network(wanted.strip(), strict=False)
    except ValueError:
        return False
    return address.version == network.version and address in network


def value_matches(base: str, value: str, wanted: str, context: Context, variables: bool) -> bool:
    if base in ("StringEquals", "StringEqualsIgnoreCase"):
        expected = expand_text(wanted, context, variables)
        if expected is None:
            return False
        return value == expected if base == "StringEquals" else value.casefold() == expected.casefold()
    if base == "StringLike":
        return glob_matches(wanted, value, context, variables)
    if base in ("ArnEquals", "ArnLike"):
        return arn_matches(value, wanted, context, variables)
    if base.startswith("Numeric"):
        left, right = parse_number(value), parse_number(wanted)
        return left is not None and right is not None and COMPARE[base[len("Numeric"):]](left, right)
    if base.startswith("Date"):
        left_time, right_time = parse_time(value), parse_time(wanted)
        return left_time is not None and right_time is not None and COMPARE[base[len("Date"):]](left_time.timestamp(), right_time.timestamp())
    if base == "Bool":
        return value.lower() == wanted.lower()
    if base == "BinaryEquals":
        return value == wanted
    if base == "IpAddress":
        return ip_in(value, wanted)
    raise PolicyError(f"condition operator {base} cannot compare values")


def parse_operator(name: str) -> Tuple[str, str, bool]:
    qualifier, _, base = name.rpartition(":")
    if qualifier and qualifier not in ("ForAllValues", "ForAnyValue"):
        raise PolicyError(f"unknown condition set operator {qualifier!r} in {name!r}: use ForAllValues or ForAnyValue")
    if_exists = base.endswith("IfExists") and base != "IfExists"
    if if_exists:
        base = base[: -len("IfExists")]
    if base not in POSITIVE and base not in NEGATED:
        raise PolicyError(f"unknown condition operator {name!r}")
    if base == "Null" and (if_exists or qualifier):
        raise PolicyError(f"{name!r}: Null takes neither IfExists nor a set operator")
    return qualifier, base, if_exists


def key_holds(operator: str, key: str, wanted: Sequence[str], context: Context, variables: bool) -> bool:
    qualifier, base, if_exists = parse_operator(operator)
    present = context.get(key)
    if base == "Null":
        absent = not present
        return any((value.lower() == "true") == absent for value in wanted)
    if not present or (qualifier == "ForAllValues" and all(value == "" for value in present)):
        if if_exists or qualifier == "ForAllValues":
            return True
        if qualifier == "ForAnyValue":
            return False
        return base in NEGATED
    negated = base in NEGATED
    positive = NEGATED.get(base, base)
    use_variables = variables and positive.startswith(("String", "Arn"))

    def one(value: str) -> bool:
        hit = any(value_matches(positive, value, item, context, use_variables) for item in wanted)
        return not hit if negated else hit

    if qualifier == "ForAllValues":
        return all(one(value) for value in present)
    if qualifier == "ForAnyValue":
        return any(one(value) for value in present)
    return all(one(value) for value in present) if negated else any(one(value) for value in present)


def condition_result(block: object, context: Context, variables: bool) -> Tuple[bool, List[str], str]:
    if not block:
        return True, [], ""
    if not isinstance(block, dict):
        raise PolicyError("Condition must be an object of operators")
    missing: List[str] = []
    failed = ""
    for operator, keys in block.items():
        if not isinstance(keys, dict):
            raise PolicyError(f"condition operator {operator} must map context keys to values")
        for key, expected in keys.items():
            if context.get(key) is None and key not in missing:
                missing.append(key)
            if not key_holds(operator, key, [context_text(item) for item in as_list(expected)], context, variables) and not failed:
                failed = f"{operator} {key}"
    return not failed, missing, failed


@dataclass
class Principal:
    arn: str
    kind: str
    account: str = ""
    name: str = ""
    role_arn: str = ""
    tags: Dict[str, str] = field(default_factory=dict)

    @property
    def is_session(self) -> bool:
        return self.kind in ("AssumedRole", "FederatedUser")

    @property
    def principal_arn(self) -> str:
        return self.role_arn if self.kind == "AssumedRole" else self.arn

    def to_json(self) -> dict:
        return {"arn": self.arn, "type": self.kind, "account": self.account, "principal_arn": self.principal_arn, "tags": dict(self.tags)}


def parse_principal(text: str) -> Principal:
    if text in ("*", "anonymous"):
        return Principal("anonymous", "Anonymous")
    if ACCOUNT_ID.match(text):
        text = f"arn:aws:iam::{text}:root"
    match = PRINCIPAL_ARN.match(text)
    if not match:
        raise PolicyError(f"not a principal ARN: {text!r} (an IAM user arn:aws:iam::ACCOUNT:user/NAME, a role session arn:aws:sts::ACCOUNT:assumed-role/ROLE/SESSION, a federated user arn:aws:sts::ACCOUNT:federated-user/NAME, arn:aws:iam::ACCOUNT:root, or anonymous)")
    partition, service, account, resource = match.group("partition", "service", "account", "resource")
    if service == "iam" and resource == "root":
        return Principal(text, "Account", account, account)
    if service == "iam" and resource.startswith("user/"):
        return Principal(text, "User", account, resource.rsplit("/", 1)[-1])
    if service == "iam" and resource.startswith("role/"):
        return Principal(text, "AssumedRole", account, resource.rsplit("/", 1)[-1], text)
    parts = resource.split("/")
    if service == "sts" and parts[0] == "assumed-role" and len(parts) == 3:
        return Principal(text, "AssumedRole", account, parts[1], f"arn:{partition}:iam::{account}:role/{parts[1]}")
    if service == "sts" and parts[0] == "federated-user" and len(parts) == 2:
        return Principal(text, "FederatedUser", account, parts[1])
    raise PolicyError(f"cannot tell what kind of principal {text!r} is")


def principal_value_grant(key: str, value: str, principal: Principal) -> str:
    if key != "AWS":
        return "service" if value == principal.arn else ""
    if value == "*":
        return "wildcard"
    if principal.kind == "Anonymous":
        return ""
    if ACCOUNT_ID.match(value):
        value = f"arn:aws:iam::{value}:root"
    match = PRINCIPAL_ARN.match(value)
    if not match or match.group("account") != principal.account:
        return ""
    resource = match.group("resource")
    if resource == "root":
        return "account"
    if resource.startswith("user/") and principal.kind == "User" and resource.rsplit("/", 1)[-1] == principal.name:
        return "user"
    if resource.startswith("role/") and principal.kind == "AssumedRole" and resource.rsplit("/", 1)[-1] == principal.name:
        return "role"
    if resource.startswith(("assumed-role/", "federated-user/")) and value == principal.arn:
        return "session"
    return ""


def principal_grant(element: object, principal: Principal) -> str:
    if element == "*":
        return "wildcard"
    if not isinstance(element, dict):
        raise PolicyError(f"Principal must be \"*\" or an object such as {{\"AWS\": ARN}}, not {element!r}")
    best = ""
    for key, values in element.items():
        for value in as_list(values):
            grant = principal_value_grant(key, str(value), principal)
            if grant and GRANT_RANK[grant] > GRANT_RANK.get(best, 0):
                best = grant
    return best


@dataclass
class Policy:
    kind: str
    name: str
    document: dict
    via: str = ""

    @property
    def variables(self) -> bool:
        return str(self.document.get("Version", "")) == VARIABLES_VERSION

    @property
    def statements(self) -> List[dict]:
        body = self.document.get("Statement")
        return [body] if isinstance(body, dict) else list(body or [])

    def describe(self) -> str:
        return f"{KIND_LABELS[self.kind]} {self.name}" + (f" ({self.via})" if self.via else "")


def decode_document(value: object) -> object:
    if not isinstance(value, str):
        return value
    text = urllib.parse.unquote(value) if value.lstrip().startswith("%7B") else value
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"policy document is not JSON: {exc}") from None


def make_policy(kind: str, name: str, document: object, via: str = "") -> Policy:
    document = decode_document(document)
    if not isinstance(document, dict) or not isinstance(document.get("Statement"), (dict, list)):
        raise PolicyError(f"{KIND_LABELS[kind]} {name} is not a policy document: it needs a Statement")
    return Policy(kind, name, document, via)


@dataclass
class Match:
    policy: Policy
    index: int
    statement: dict
    grant: str = ""

    @property
    def sid(self) -> str:
        return str(self.statement.get("Sid") or f"#{self.index + 1}")

    @property
    def effect(self) -> str:
        return str(self.statement.get("Effect"))

    def describe(self) -> str:
        return f'statement "{self.sid}" ({self.effect}) in {self.policy.describe()}'

    def to_json(self) -> dict:
        return {"SourcePolicyId": self.policy.name, "SourcePolicyType": KIND_LABELS[self.policy.kind], "Sid": self.sid, "Effect": self.effect, "StatementIndex": self.index, "PrincipalGrant": self.grant or None, "Statement": self.statement}


ROOT_ALLOW = Match(Policy("identity", "(implicit) the account root user has full access", {"Statement": []}), -1, {"Sid": "RootUser", "Effect": "Allow"})


@dataclass
class Request:
    action: str
    resource: str = "*"
    context: Dict[str, object] = field(default_factory=dict)


@dataclass
class Scenario:
    principal: Principal
    identity: List[Policy] = field(default_factory=list)
    boundary: Optional[Policy] = None
    session: Optional[List[Policy]] = None
    scps: List[List[Policy]] = field(default_factory=list)
    rcps: List[List[Policy]] = field(default_factory=list)
    resource_policy: Optional[Policy] = None
    resource_account: str = ""
    context: Dict[str, object] = field(default_factory=dict)


@dataclass
class Step:
    name: str
    outcome: str
    detail: str = ""


@dataclass
class Decision:
    request: Request
    principal: Principal
    decision: str
    kind: str
    match: Optional[Match]
    steps: List[Step]
    matched: List[Match]
    missing: List[str]
    context: Context
    resource_account: str

    @property
    def allowed(self) -> bool:
        return self.decision == "allowed"

    @property
    def cross_account(self) -> bool:
        return self.principal.kind != "Anonymous" and self.resource_account != self.principal.account

    @property
    def message(self) -> str:
        if self.allowed:
            return ""
        return access_denied_message(self.principal.arn, self.request.action, self.request.resource, self.decision, self.kind)

    def to_json(self) -> dict:
        return {
            "EvalActionName": self.request.action,
            "EvalResourceName": self.request.resource,
            "EvalDecision": self.decision,
            "DecidingPolicyType": KIND_LABELS.get(self.kind),
            "DecidingStatement": self.match.to_json() if self.match else None,
            "MatchedStatements": [m.to_json() for m in self.matched],
            "MissingContextValues": self.missing,
            "Message": self.message or None,
            "CrossAccount": self.cross_account,
            "ResourceAccount": self.resource_account,
            "Principal": self.principal.to_json(),
            "Context": self.context.to_json(),
            "Steps": [{"name": s.name, "outcome": s.outcome, "detail": s.detail} for s in self.steps],
        }


def access_denied_message(principal_arn: str, action: str, resource: str, decision: str, kind: str) -> str:
    text = f"User: {principal_arn} is not authorized to perform: {action}"
    if resource and resource != "*":
        text += f" on resource: {resource}"
    if decision == "explicitDeny":
        return f"{text} with an explicit deny in {KIND_ARTICLES.get(kind, 'a')} {KIND_LABELS[kind]}"
    return f"{text} because no {KIND_LABELS[kind]} allows the {action} action"


def account_of(arn: str) -> str:
    parts = arn.split(":", 5)
    return parts[4] if len(parts) == 6 and ACCOUNT_ID.match(parts[4]) else ""


def request_context(scenario: Scenario, request: Request, now: datetime, resource_account: str) -> Context:
    principal = scenario.principal
    context = Context({"aws:PrincipalType": principal.kind, "aws:SecureTransport": "true", "aws:CurrentTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "aws:EpochTime": str(int(now.timestamp()))})
    if principal.kind != "Anonymous":
        context.update({"aws:PrincipalArn": principal.principal_arn, "aws:PrincipalAccount": principal.account})
    if principal.kind == "User":
        context.set("aws:username", principal.name)
    for key, value in principal.tags.items():
        context.set(f"aws:PrincipalTag/{key}", value)
    if resource_account:
        context.set("aws:ResourceAccount", resource_account)
    context.update(scenario.context)
    context.update(request.context)
    return context


def statement_applies(policy: Policy, index: int, statement: dict, request: Request, principal: Principal, context: Context, has_boundary: bool, missing: List[str]) -> Optional[Match]:
    if "NotAction" in statement:
        if any(action_matches(p, request.action) for p in as_list(statement["NotAction"])):
            return None
    elif not any(action_matches(p, request.action) for p in as_list(statement.get("Action"))):
        return None
    variables = policy.variables
    if "NotResource" in statement:
        if any(resource_matches(p, request.resource, context, variables) for p in as_list(statement["NotResource"])):
            return None
    elif "Resource" in statement:
        if not any(resource_matches(p, request.resource, context, variables) for p in as_list(statement["Resource"])):
            return None
    elif policy.kind != "trust":
        return None
    grant = ""
    if policy.kind in ("resource", "trust"):
        if "Principal" in statement:
            grant = principal_grant(statement["Principal"], principal)
        elif "NotPrincipal" in statement:
            if statement.get("Effect") == "Deny" and has_boundary:
                grant = "boundary-notprincipal"
            elif not principal_grant(statement["NotPrincipal"], principal):
                grant = "wildcard"
        if not grant:
            return None
    holds, absent, _ = condition_result(statement.get("Condition"), context, variables)
    missing.extend(key for key in absent if key not in missing)
    return Match(policy, index, statement, grant) if holds else None


class Evaluation:
    def __init__(self, scenario: Scenario, request: Request, now: datetime) -> None:
        self.scenario = scenario
        self.request = request
        self.principal = scenario.principal
        self.resource_account = scenario.resource_account or account_of(request.resource) or self.principal.account
        self.context = request_context(scenario, request, now, self.resource_account)
        self.steps: List[Step] = []
        self.matched: List[Match] = []
        self.missing: List[str] = []

    def find(self, policies: Sequence[Policy], effect: str) -> List[Match]:
        found = []
        for policy in policies:
            for index, statement in enumerate(policy.statements):
                if not isinstance(statement, dict):
                    raise PolicyError(f"statement {index + 1} of {policy.describe()} is not an object")
                if statement.get("Effect") != effect:
                    continue
                match = statement_applies(policy, index, statement, self.request, self.principal, self.context, self.scenario.boundary is not None, self.missing)
                if match:
                    found.append(match)
        self.matched.extend(found)
        return found

    def step(self, name: str, outcome: str, detail: str = "") -> None:
        self.steps.append(Step(name, outcome, detail))

    def finish(self, decision: str, kind: str, match: Optional[Match] = None) -> Decision:
        done = {step.name for step in self.steps}
        self.steps.extend(Step(name, "not evaluated") for name in STEP_NAMES if name not in done)
        return Decision(self.request, self.principal, decision, kind, match, self.steps, self.matched, self.missing, self.context, self.resource_account)

    def organization_gate(self, name: str, kind: str, levels: Sequence[Sequence[Policy]]) -> Optional[Decision]:
        if not levels:
            self.step(name, "skip", "none apply")
            return None
        for number, level in enumerate(levels, 1):
            if not self.find(level, "Allow"):
                self.step(name, "implicit deny", f"level {number} of {len(levels)} ({', '.join(p.name for p in level)}) has no Allow that applies")
                return self.finish("implicitDeny", kind)
        self.step(name, "pass", f"every level allows ({len(levels)})")
        return None


def evaluate(scenario: Scenario, request: Request, now: Optional[datetime] = None) -> Decision:
    run = Evaluation(scenario, request, now or datetime.now(timezone.utc))
    principal, resource_policy = scenario.principal, scenario.resource_policy
    scps = [] if principal.kind == "Anonymous" else scenario.scps
    principal_side = scenario.identity + ([scenario.boundary] if scenario.boundary else []) + (scenario.session or [])
    resource_side = [resource_policy] if resource_policy else []
    everything = sorted(principal_side + resource_side + [p for level in list(scps) + list(scenario.rcps) for p in level], key=lambda p: DENY_ORDER.index(p.kind))

    denies = run.find(everything, "Deny")
    if denies:
        run.step("explicit deny", "explicit deny", denies[0].describe())
        return run.finish("explicitDeny", denies[0].policy.kind, denies[0])
    run.step("explicit deny", "pass", f"no Deny statement applies ({len(everything)} {'policy' if len(everything) == 1 else 'policies'})")

    for name, kind, levels in (("resource control policies", "rcp", scenario.rcps), ("service control policies", "scp", scps)):
        stopped = run.organization_gate(name, kind, levels)
        if stopped:
            return stopped

    requires_trust = request.action.lower() in ASSUME_ACTIONS
    resource_kind = resource_policy.kind if resource_policy else ("trust" if requires_trust else "resource")
    grants = run.find(resource_side, "Allow")
    direct = [g for g in grants if g.grant in DIRECT_GRANTS]
    via_role = [g for g in grants if g.grant == "role"]
    cross = principal.kind == "Anonymous" or run.resource_account != principal.account
    if direct and not cross:
        run.step("resource-based policy", "allow", f"{direct[0].describe()} grants this {direct[0].grant} directly; identity-based policies, boundaries and session policies do not limit it in the same account")
        return run.finish("allowed", resource_kind, direct[0])
    if principal.kind == "Anonymous":
        if direct:
            run.step("resource-based policy", "allow", direct[0].describe())
            return run.finish("allowed", resource_kind, direct[0])
        run.step("resource-based policy", "implicit deny", "an anonymous request needs a resource-based policy that allows Principal \"*\"")
        return run.finish("implicitDeny", resource_kind)
    if (cross or requires_trust) and not grants:
        why = f"the resource is in account {run.resource_account}: its {KIND_LABELS[resource_kind]} must allow this principal" if cross else f"{KIND_LABELS[resource_kind]} must name this principal, even in the same account"
        run.step("resource-based policy", "implicit deny", why if resource_policy else why + " (none given)")
        return run.finish("implicitDeny", resource_kind)
    if grants:
        meaning = {"role": "grants the role ARN, so the boundary and session policies still apply", "account": "names the account, which delegates the decision to identity-based policies"}
        run.step("resource-based policy", "pass", f"{grants[0].describe()} {meaning.get(grants[0].grant, '')}".strip())
    else:
        run.step("resource-based policy", "skip", "no statement allows; the same account can be allowed by identity-based policies alone" if resource_policy else "none given")

    identity = [ROOT_ALLOW] if principal.kind == "Account" else run.find(scenario.identity, "Allow")
    if identity:
        run.step("identity-based policies", "pass", identity[0].describe())
    elif cross or not via_role:
        run.step("identity-based policies", "implicit deny", f"no Allow for {request.action} in {len(scenario.identity)} identity-based policies")
        return run.finish("implicitDeny", "identity")
    else:
        run.step("identity-based policies", "skip", "not needed: the resource-based policy grants the role")

    if scenario.boundary is not None and principal.kind in ("User", "AssumedRole"):
        allows = run.find([scenario.boundary], "Allow")
        if not allows:
            run.step("permissions boundary", "implicit deny", f"{scenario.boundary.name} sets the maximum and does not allow {request.action}")
            return run.finish("implicitDeny", "boundary")
        run.step("permissions boundary", "pass", allows[0].describe())
    else:
        run.step("permissions boundary", "skip", "none attached")

    if principal.is_session and scenario.session is not None:
        allows = run.find(scenario.session, "Allow")
        if not allows:
            run.step("session policies", "implicit deny", f"the {len(scenario.session)} session policies passed when the session was created do not allow {request.action}")
            return run.finish("implicitDeny", "session")
        run.step("session policies", "pass", allows[0].describe())
    elif principal.kind == "FederatedUser":
        run.step("session policies", "implicit deny", "GetFederationToken without a session policy grants nothing")
        return run.finish("implicitDeny", "session")
    else:
        run.step("session policies", "skip", "no session policy was passed" if principal.is_session else "not a session")

    decided = identity[0] if identity else via_role[0]
    return run.finish("allowed", decided.policy.kind, decided)


@dataclass
class Directory:
    account: str
    users: Dict[str, dict]
    groups: Dict[str, dict]
    roles: Dict[str, dict]
    managed: Dict[str, Tuple[str, object]]


def load_directory(doc: object) -> Directory:
    if not isinstance(doc, dict) or not any(key in doc for key in ("UserDetailList", "RoleDetailList", "GroupDetailList", "Policies")):
        raise PolicyError(f"not the output of aws iam get-account-authorization-details (make one with: {DUMP_COMMAND})")
    managed: Dict[str, Tuple[str, object]] = {}
    for item in doc.get("Policies") or []:
        versions = item.get("PolicyVersionList") or []
        default = next((v for v in versions if v.get("IsDefaultVersion")), None) or next((v for v in versions if v.get("VersionId") == item.get("DefaultVersionId")), None)
        if item.get("Arn") and default:
            managed[item["Arn"]] = (item.get("PolicyName") or item["Arn"].rsplit("/", 1)[-1], default.get("Document"))
    users = {u["UserName"]: u for u in doc.get("UserDetailList") or []}
    groups = {g["GroupName"]: g for g in doc.get("GroupDetailList") or []}
    roles = {r["RoleName"]: r for r in doc.get("RoleDetailList") or []}
    arns = [entry.get("Arn", "") for entry in list(users.values()) + list(groups.values()) + list(roles.values())] + list(managed)
    account = next((account_of(arn) for arn in arns if account_of(arn)), "")
    return Directory(account, users, groups, roles, managed)


def directory_policies(directory: Directory, principal: Principal) -> Tuple[List[Policy], Optional[Policy], List[str]]:
    warnings: List[str] = []

    def inline(entries: object, owner: str) -> List[Policy]:
        return [make_policy("identity", f"{owner}/{e['PolicyName']}", e["PolicyDocument"]) for e in as_list(entries)]

    def attached(entries: object, via: str, kind: str = "identity") -> List[Policy]:
        found = []
        for entry in as_list(entries):
            arn = entry["PolicyArn"] if isinstance(entry, dict) else str(entry)
            if arn not in directory.managed:
                warnings.append(f"managed policy {arn} ({via}) is not in the dump; it was ignored")
                continue
            found.append(make_policy(kind, arn, directory.managed[arn][1], via))
        return found

    if principal.kind == "User":
        detail = directory.users.get(principal.name)
        if detail is None:
            return [], None, [f"user {principal.name} is not in the dump"]
        policies = inline(detail.get("UserPolicyList"), f"user/{principal.name}") + attached(detail.get("AttachedManagedPolicies"), "attached to the user")
        for group in detail.get("GroupList") or []:
            if group not in directory.groups:
                warnings.append(f"group {group} is not in the dump")
                continue
            entry = directory.groups[group]
            policies += inline(entry.get("GroupPolicyList"), f"group/{group}") + attached(entry.get("AttachedManagedPolicies"), f"via group {group}")
    elif principal.kind == "AssumedRole":
        detail = directory.roles.get(principal.name)
        if detail is None:
            return [], None, [f"role {principal.name} is not in the dump"]
        principal.role_arn = detail.get("Arn") or principal.role_arn
        policies = inline(detail.get("RolePolicyList"), f"role/{principal.name}") + attached(detail.get("AttachedManagedPolicies"), "attached to the role")
    else:
        return [], None, []
    principal.tags.update({t["Key"]: t["Value"] for t in detail.get("Tags") or []})
    boundary_arn = (detail.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn")
    boundaries = attached([boundary_arn], "permissions boundary", "boundary") if boundary_arn else []
    return policies, (boundaries[0] if boundaries else None), warnings


def directory_trust(directory: Directory, role_arn: str) -> Optional[Policy]:
    match = PRINCIPAL_ARN.match(role_arn)
    if not match or match.group("account") != directory.account or not match.group("resource").startswith("role/"):
        return None
    name = match.group("resource").rsplit("/", 1)[-1]
    detail = directory.roles.get(name)
    return make_policy("trust", f"role/{name}", detail["AssumeRolePolicyDocument"]) if detail else None


def read_text(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    path = source[len("file://"):] if source.startswith("file://") else source
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise PolicyError(f"cannot read {path}: {exc.strerror}") from None


def read_json(source: str) -> object:
    text = source if source.lstrip().startswith(("{", "[")) else read_text(source)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"{source if len(source) < 60 else 'inline JSON'} is not JSON: {exc}") from None


def source_name(source: str, kind: str, number: int) -> str:
    return f"{kind}-{number}" if source.lstrip().startswith(("{", "[")) or source == "-" else os.path.basename(source)


def named_policy(kind: str, item: object, default: str) -> Policy:
    if isinstance(item, dict) and "document" in item:
        return make_policy(kind, str(item.get("name") or default), item["document"])
    name = str(item.get("Id") or default) if isinstance(item, dict) else default
    return make_policy(kind, name, item)


def policy_level(kind: str, item: object, default: str) -> List[Policy]:
    items = item if isinstance(item, list) else [item]
    return [named_policy(kind, entry, f"{default}.{n}" if len(items) > 1 else default) for n, entry in enumerate(items, 1)]


def scenario_from_json(doc: dict) -> Scenario:
    if not isinstance(doc, dict) or "principal" not in doc:
        raise PolicyError("a scenario is an object with at least a principal and requests")
    principal = parse_principal(str(doc["principal"]))
    principal.tags.update(doc.get("principal_tags") or {})
    sessions = doc.get("session")
    resource = doc.get("resource_policy")
    return Scenario(
        principal,
        [named_policy("identity", item, f"identity-{n}") for n, item in enumerate(doc.get("identity") or [], 1)],
        named_policy("boundary", doc["boundary"], "boundary") if doc.get("boundary") else None,
        None if sessions is None else [named_policy("session", item, f"session-{n}") for n, item in enumerate(sessions, 1)],
        [policy_level("scp", level, f"scp-level-{n}") for n, level in enumerate(doc.get("scps") or [], 1)],
        [policy_level("rcp", level, f"rcp-level-{n}") for n, level in enumerate(doc.get("rcps") or [], 1)],
        named_policy(doc.get("resource_policy_type", "resource"), resource, "resource-policy") if resource else None,
        str(doc.get("resource_account") or ""),
        dict(doc.get("context") or {}),
    )


def parse_context(pairs: Sequence[str]) -> Dict[str, object]:
    values: Dict[str, List[str]] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise PolicyError(f"context must be KEY=VALUE, got {pair!r} (repeat a key for a multivalued key such as aws:TagKeys)")
        values.setdefault(key, []).append(value)
    return {key: (items[0] if len(items) == 1 else items) for key, items in values.items()}


def scenario_from_args(args: argparse.Namespace) -> Tuple[Scenario, Optional[Directory], List[str]]:
    principal = parse_principal(args.principal)
    identity = [make_policy("identity", source_name(s, "policy", n), read_json(s)) for n, s in enumerate(args.policy, 1)]
    boundary = make_policy("boundary", source_name(args.boundary, "boundary", 1), read_json(args.boundary)) if args.boundary else None
    directory, warnings = None, []
    if args.auth_details:
        directory = load_directory(read_json(args.auth_details))
        found, dumped_boundary, warnings = directory_policies(directory, principal)
        identity, boundary = found + identity, boundary or dumped_boundary
    session = [make_policy("session", source_name(s, "session", n), read_json(s)) for n, s in enumerate(args.session_policy, 1)] or None
    scps = [policy_level("scp", read_json(s), source_name(s, "scp", n)) for n, s in enumerate(args.scp, 1)]
    rcps = [policy_level("rcp", read_json(s), source_name(s, "rcp", n)) for n, s in enumerate(args.rcp, 1)]
    resource_policy = make_policy(args.resource_policy_type, source_name(args.resource_policy, "resource-policy", 1), read_json(args.resource_policy)) if args.resource_policy else None
    return Scenario(principal, identity, boundary, session, scps, rcps, resource_policy, args.resource_account or "", parse_context(args.context)), directory, warnings


def with_trust(scenario: Scenario, directory: Optional[Directory], request: Request) -> Scenario:
    if scenario.resource_policy is None and directory and request.action.lower() in ASSUME_ACTIONS:
        trust = directory_trust(directory, request.resource)
        if trust:
            return replace(scenario, resource_policy=trust)
    return scenario


@dataclass
class Finding:
    severity: str
    code: str
    where: str
    message: str

    def to_json(self) -> dict:
        return {"findingType": self.severity, "code": self.code, "location": self.where, "message": self.message}


def single_valued(key: str) -> bool:
    lower = key.lower()
    return lower.startswith(SINGLE_VALUED_PREFIXES) or lower in SINGLE_VALUED_KEYS


def validate_policy(document: object, kind: str = "identity") -> List[Finding]:
    findings: List[Finding] = []

    def add(severity: str, code: str, where: str, message: str) -> None:
        findings.append(Finding(severity, code, where, message))

    if not isinstance(document, dict):
        return [Finding("ERROR", "not-an-object", "policy", "a policy document is a JSON object")]
    version = str(document.get("Version", ""))
    if not version:
        add("WARNING", "missing-version", "Version", "without Version 2012-10-17, policy variables such as ${aws:username} are matched as literal text")
    elif version not in KNOWN_VERSIONS:
        add("ERROR", "invalid-version", "Version", f"Version must be 2012-10-17 (or the legacy 2008-10-17), not {version}")
    body = document.get("Statement")
    statements = [body] if isinstance(body, dict) else body if isinstance(body, list) else None
    if not statements:
        return findings + [Finding("ERROR", "missing-statement", "Statement", "a policy needs at least one statement")]
    sids: List[str] = []
    for index, statement in enumerate(statements):
        where = f"Statement[{index}]" + (f" ({statement.get('Sid')})" if isinstance(statement, dict) and statement.get("Sid") else "")
        if not isinstance(statement, dict):
            add("ERROR", "not-an-object", where, "a statement is a JSON object")
            continue
        if statement.get("Sid"):
            if statement["Sid"] in sids:
                add("WARNING", "duplicate-sid", where, f"Sid {statement['Sid']} is used twice; tools and error messages can no longer tell the statements apart")
            sids.append(statement["Sid"])
        effect = statement.get("Effect")
        if effect not in ("Allow", "Deny"):
            add("ERROR", "invalid-effect", where, f"Effect must be Allow or Deny (case-sensitive), not {effect!r}")
        if ("Action" in statement) == ("NotAction" in statement):
            add("ERROR", "action-xor-notaction", where, "a statement needs exactly one of Action and NotAction")
        for action in as_list(statement.get("Action")) + as_list(statement.get("NotAction")):
            if str(action) != "*" and not re.fullmatch(r"[A-Za-z0-9-]+:[A-Za-z0-9*?]+", str(action)):
                add("ERROR", "invalid-action", where, f"{action!r} is not service:Action (wildcards * and ? are allowed after the colon)")
        if "Resource" in statement and "NotResource" in statement:
            add("ERROR", "resource-and-notresource", where, "use Resource or NotResource, not both")
        elif "Resource" not in statement and "NotResource" not in statement and kind != "trust":
            add("ERROR", "missing-resource", where, "only role trust policies may omit Resource")
        has_principal = "Principal" in statement or "NotPrincipal" in statement
        if kind in ("identity", "boundary", "session", "scp") and has_principal:
            add("ERROR", "principal-not-allowed", where, f"a {KIND_LABELS[kind]} applies to the identity it is attached to; Principal belongs in resource-based policies")
        if kind in ("resource", "trust", "rcp") and not has_principal:
            add("ERROR", "missing-principal", where, f"a {KIND_LABELS[kind]} statement must say who it applies to (Principal)")
        principals = statement.get("Principal")
        values = [principals] if principals == "*" else [str(v) for vs in (principals.values() if isinstance(principals, dict) else []) for v in as_list(vs)]
        for value in values:
            if UNIQUE_ID.match(value):
                add("WARNING", "deleted-principal", where, f"{value} is a unique ID: IAM shows one when the user or role it named was deleted; recreating that name does not restore access")
        if effect == "Allow":
            if "*" in [str(a) for a in as_list(statement.get("Action"))] and "*" in [str(r) for r in as_list(statement.get("Resource"))]:
                add("SECURITY_WARNING", "allow-all", where, "Allow * on * grants every action in every service, including changing this policy")
            if "NotAction" in statement:
                add("SECURITY_WARNING", "allow-with-notaction", where, "Allow with NotAction grants every action not listed, including actions AWS adds later")
            if "NotResource" in statement:
                add("SECURITY_WARNING", "allow-with-notresource", where, "Allow with NotResource grants the actions on every resource not listed")
            if "*" in values and not statement.get("Condition"):
                add("SECURITY_WARNING", "public-access", where, "Principal * without a Condition allows anyone, including anonymous callers")
        variables_used = "${" in json.dumps({k: statement.get(k) for k in ("Resource", "NotResource", "Condition")})
        if variables_used and version != VARIABLES_VERSION:
            add("WARNING", "variables-are-literal", where, "policy variables are only substituted with Version 2012-10-17; here ${...} is literal text")
        condition = statement.get("Condition") or {}
        for operator, keys in (condition.items() if isinstance(condition, dict) else []):
            try:
                qualifier, base, _ = parse_operator(operator)
            except PolicyError as exc:
                add("ERROR", "invalid-condition-operator", where, str(exc))
                continue
            for key in (keys if isinstance(keys, dict) else {}):
                if qualifier and single_valued(key):
                    add("WARNING", "set-operator-on-single-valued-key", where, f"{operator} on the single-valued key {key}: drop {qualifier}; with ForAllValues a missing key matches")
                if not qualifier and base != "Null" and key.lower() in MULTIVALUED_KEYS:
                    add("WARNING", "multivalued-key-without-set-operator", where, f"{key} is multivalued: use ForAnyValue or ForAllValues with {operator}")
                if qualifier == "ForAllValues" and effect == "Allow" and not any(key.lower() == k.lower() for k in (condition.get("Null") or {})):
                    add("SECURITY_WARNING", "forallvalues-without-null", where, f"ForAllValues is true when {key} is absent; add \"Null\": {{\"{key}\": \"false\"}}")
    return findings


def explain_message(text: str) -> dict:
    event = None
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            event = None
    message = " ".join(str(event.get("errorMessage", "")).split()) if isinstance(event, dict) else " ".join(stripped.split())
    match = DENIED.search(message.rstrip("."))
    if not match:
        return {"recognized": False, "message": message, "next": "Not an IAM AccessDenied message. Look for 'is not authorized to perform' (most services) or an S3 'Access Denied' with its request ID."}
    reason = match.group("reason") or ""
    explicit, implicit = EXPLICIT.match(reason), IMPLICIT.match(reason)
    found = explicit or implicit
    label = found.group("label") if found else ""
    kind = next((k for k, v in KIND_LABELS.items() if v == label), EXTRA_LABELS.get(label, ""))
    principal = match.group("principal")
    try:
        principal_type = parse_principal(principal).kind
    except PolicyError:
        principal_type = "unknown"
    resource = match.group("resource") or "*"
    result = {
        "recognized": True,
        "principal": principal,
        "principal_type": principal_type,
        "action": match.group("action"),
        "resource": resource,
        "decision": "explicitDeny" if explicit else "implicitDeny" if implicit else "unknown",
        "policy_type": label or None,
        "policy_arn": explicit.group("arn") if explicit else None,
        "next": NEXT_STEPS.get(kind, NEXT_STEPS[""]),
        "iameval": f"python3 code/main.py eval --principal {principal} --action {match.group('action')} --resource '{resource}' --auth-details account.json",
    }
    if isinstance(event, dict):
        result["event"] = {k: event.get(k) for k in ("eventTime", "eventSource", "eventName", "errorCode", "sourceIPAddress")}
    return result


def render_decision(decision: Decision) -> str:
    principal = decision.principal
    lines = [f"{decision.decision}  {decision.request.action} on {decision.request.resource}"]
    where = f"; resource account {decision.resource_account}, so both accounts must allow" if decision.cross_account else ""
    lines.append(f"  principal {principal.arn} ({principal.kind}{', account ' + principal.account if principal.account else ''}){where}")
    width = max(len(step.name) for step in decision.steps)
    for number, step in enumerate(decision.steps, 1):
        lines.append(f"  {number}. {step.name.ljust(width)}  {step.outcome.ljust(13)}  {step.detail}".rstrip())
    if decision.missing:
        lines.append(f"  context keys not in the request: {', '.join(decision.missing)}")
    if not decision.allowed:
        lines.append(f"  AccessDenied: {decision.message}")
    return "\n".join(lines)


def run_eval(args: argparse.Namespace) -> int:
    scenario, directory, warnings = scenario_from_args(args)
    for warning in warnings:
        print(f"iameval: warning: {warning}", file=sys.stderr)
    now = parse_time(args.time) if args.time else None
    if args.time and now is None:
        raise PolicyError(f"--time must be ISO 8601 or epoch seconds, not {args.time!r}")
    decisions = []
    for action in args.action:
        for resource in args.resource or ["*"]:
            request = Request(action, resource)
            decisions.append(evaluate(with_trust(scenario, directory, request), request, now))
    if args.json:
        print(json.dumps({"EvaluationResults": [d.to_json() for d in decisions], "Warnings": warnings}, indent=2))
    else:
        print("\n\n".join(render_decision(d) for d in decisions))
    return 0 if all(d.allowed for d in decisions) else 1


def run_scenarios(args: argparse.Namespace) -> int:
    results = []
    for path in args.files:
        doc = read_json(path)
        for item in doc.get("scenarios", []) if isinstance(doc, dict) else doc:
            scenario = scenario_from_json(item)
            now = parse_time(item["time"]) if item.get("time") else None
            for request_doc in item.get("requests") or []:
                request = Request(request_doc["action"], request_doc.get("resource", "*"), dict(request_doc.get("context") or {}))
                decision = evaluate(scenario, request, now)
                problems = []
                if request_doc.get("expect") and decision.decision != request_doc["expect"]:
                    problems.append(f"expected {request_doc['expect']}, got {decision.decision}")
                if request_doc.get("expect_type") and decision.kind != request_doc["expect_type"]:
                    problems.append(f"expected the {KIND_LABELS.get(request_doc['expect_type'], request_doc['expect_type'])} to decide, got the {KIND_LABELS.get(decision.kind, decision.kind)}")
                if request_doc.get("expect_sid") and (not decision.match or decision.match.sid != request_doc["expect_sid"]):
                    problems.append(f"expected statement {request_doc['expect_sid']}, got {decision.match.sid if decision.match else 'none'}")
                if request_doc.get("expect_message") and decision.message != request_doc["expect_message"]:
                    problems.append(f"message differs: {decision.message}")
                results.append({"scenario": item.get("name", ""), "source": item.get("source", ""), "action": request.action, "resource": request.resource, "decision": decision.decision, "policy_type": KIND_LABELS.get(decision.kind), "ok": not problems, "problems": problems})
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for row in results:
            print(f"{'ok  ' if row['ok'] else 'FAIL'}  {row['decision']:<13} {row['action']} {row['resource']}  [{row['scenario']}]" + "".join(f"\n      {p}" for p in row["problems"]))
        print(f"{sum(r['ok'] for r in results)}/{len(results)} decisions as documented")
    return 0 if all(r["ok"] for r in results) else 1


def run_validate(args: argparse.Namespace) -> int:
    findings = validate_policy(read_json(args.file), args.type)
    if args.json:
        print(json.dumps({"findings": [f.to_json() for f in findings]}, indent=2))
    else:
        for finding in findings:
            print(f"{finding.severity:<16} {finding.code:<38} {finding.where}: {finding.message}")
        print(f"{len(findings)} finding(s)" if findings else "no findings")
    return 1 if any(f.severity in ("ERROR", "SECURITY_WARNING") for f in findings) else 0


def run_explain(args: argparse.Namespace) -> int:
    result = explain_message(read_text("-") if args.message == "-" else args.message)
    if args.json:
        print(json.dumps(result, indent=2))
    elif not result["recognized"]:
        print(result["next"])
    else:
        rows = [("principal", f"{result['principal']} ({result['principal_type']})"), ("action", result["action"]), ("resource", result["resource"]), ("decision", result["decision"]), ("policy type", result["policy_type"] or "not stated by the service")]
        if result["policy_arn"]:
            rows.append(("policy", result["policy_arn"]))
        print("\n".join(f"{name:<12} {value}" for name, value in rows))
        print(f"next         {result['next']}\nreproduce    {result['iameval']}")
    return 0 if result["recognized"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iameval", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    ev = sub.add_parser("eval", help="decide requests like aws iam simulate-principal-policy, with the reason")
    ev.add_argument("--principal", required=True, help="the caller: arn:aws:iam::ACCOUNT:user/NAME, arn:aws:sts::ACCOUNT:assumed-role/ROLE/SESSION, arn:aws:sts::ACCOUNT:federated-user/NAME, arn:aws:iam::ACCOUNT:root, or anonymous")
    ev.add_argument("--action", action="append", required=True, help="service:Action, repeatable")
    ev.add_argument("--resource", action="append", default=[], help="resource ARN, repeatable (default *)")
    ev.add_argument("--auth-details", metavar="FILE", help=f"take the principal's policies, groups, boundary, tags and role trust policies from: {DUMP_COMMAND}")
    ev.add_argument("--policy", action="append", default=[], metavar="FILE|JSON", help="an identity-based policy, repeatable (like simulate-custom-policy --policy-input-list)")
    ev.add_argument("--boundary", metavar="FILE|JSON", help="the permissions boundary")
    ev.add_argument("--session-policy", action="append", default=[], metavar="FILE|JSON", help="a session policy passed to AssumeRole or GetFederationToken, repeatable")
    ev.add_argument("--scp", action="append", default=[], metavar="FILE|JSON", help="the SCPs attached at one level (root, OU, account): a policy or a JSON array of them; repeat once per level")
    ev.add_argument("--rcp", action="append", default=[], metavar="FILE|JSON", help="the RCPs attached at one level, like --scp")
    ev.add_argument("--resource-policy", metavar="FILE|JSON", help="the resource-based policy (bucket policy, key policy, queue policy, role trust policy)")
    ev.add_argument("--resource-policy-type", choices=("resource", "trust"), default="resource", help="trust for a role trust policy (sts:AssumeRole)")
    ev.add_argument("--resource-account", help="the account that owns the resource; S3 ARNs carry none (default: the ARN's account, else the principal's)")
    ev.add_argument("-c", "--context", action="append", default=[], metavar="KEY=VALUE", help="request context, e.g. aws:SourceIp=203.0.113.7; repeat a key for multivalued keys")
    ev.add_argument("--time", help="aws:CurrentTime as ISO 8601 or epoch seconds (default: now)")
    ev.add_argument("--json", action="store_true", help="EvaluationResults in the shape of the simulate APIs, plus the deciding statement and message")
    sc = sub.add_parser("scenarios", help="evaluate scenario files and compare with the decisions they expect")
    sc.add_argument("files", nargs="+")
    sc.add_argument("--json", action="store_true")
    va = sub.add_parser("validate", help="lint a policy the way IAM Access Analyzer's validate-policy does (a subset of its checks)")
    va.add_argument("file", help="policy file or inline JSON")
    va.add_argument("--type", choices=sorted(KIND_LABELS), default="identity", help="what kind of policy it is (default identity)")
    va.add_argument("--json", action="store_true")
    ex = sub.add_parser("explain", help="read an AccessDenied message or CloudTrail event and say where to look")
    ex.add_argument("message", help="the error text or CloudTrail event JSON; - reads stdin")
    ex.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    runners = {"eval": run_eval, "scenarios": run_scenarios, "validate": run_validate, "explain": run_explain}
    try:
        return runners[args.command](args)
    except PolicyError as exc:
        print(f"iameval: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
