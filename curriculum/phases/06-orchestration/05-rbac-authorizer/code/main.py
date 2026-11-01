"""canhaz: answer `kubectl auth can-i` from a dump of a cluster's RBAC objects, name the binding and rule that decided it, list what a subject may do, find who may do something, and apply the escalation rule to a new binding."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, TextIO, Tuple

DUMP_COMMAND = "kubectl get roles,rolebindings,clusterroles,clusterrolebindings -A -o json > rbac.json"
RBAC_GROUP = "rbac.authorization.k8s.io"
PRIVILEGED_GROUP = "system:masters"
AUTHENTICATED = "system:authenticated"
UNAUTHENTICATED = "system:unauthenticated"
ANONYMOUS = "system:anonymous"
SA_PREFIX = "system:serviceaccount:"
ALL_SERVICE_ACCOUNTS = "system:serviceaccounts"
OBJECT_KINDS = ("Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding")
LIST_HEADER = ("Resources", "Non-Resource URLs", "Resource Names", "Verbs")

API_GROUPS = {
    "": ["pods", "services", "endpoints", "configmaps", "secrets", "serviceaccounts", "persistentvolumeclaims", "persistentvolumes", "nodes", "namespaces", "events", "limitranges", "resourcequotas", "replicationcontrollers", "podtemplates", "bindings", "componentstatuses", "users", "groups", "userextras", "uids"],
    "apps": ["deployments", "replicasets", "statefulsets", "daemonsets", "controllerrevisions"],
    "batch": ["jobs", "cronjobs"],
    "autoscaling": ["horizontalpodautoscalers"],
    "policy": ["poddisruptionbudgets"],
    "networking.k8s.io": ["ingresses", "ingressclasses", "networkpolicies"],
    "discovery.k8s.io": ["endpointslices"],
    "coordination.k8s.io": ["leases"],
    "storage.k8s.io": ["storageclasses", "volumeattachments", "csidrivers", "csinodes"],
    RBAC_GROUP: ["roles", "rolebindings", "clusterroles", "clusterrolebindings"],
    "authorization.k8s.io": ["selfsubjectaccessreviews", "selfsubjectrulesreviews", "subjectaccessreviews", "localsubjectaccessreviews"],
    "authentication.k8s.io": ["tokenreviews", "selfsubjectreviews"],
    "certificates.k8s.io": ["certificatesigningrequests"],
    "apiextensions.k8s.io": ["customresourcedefinitions"],
    "admissionregistration.k8s.io": ["validatingwebhookconfigurations", "mutatingwebhookconfigurations", "validatingadmissionpolicies", "validatingadmissionpolicybindings"],
}
GROUP_OF = {resource: group for group, resources in API_GROUPS.items() for resource in resources}
CLUSTER_SCOPED = {
    "nodes", "namespaces", "persistentvolumes", "componentstatuses", "clusterroles", "clusterrolebindings", "storageclasses",
    "volumeattachments", "csidrivers", "csinodes", "ingressclasses", "customresourcedefinitions", "certificatesigningrequests",
    "selfsubjectaccessreviews", "selfsubjectrulesreviews", "subjectaccessreviews", "tokenreviews", "selfsubjectreviews",
    "validatingwebhookconfigurations", "mutatingwebhookconfigurations", "validatingadmissionpolicies", "validatingadmissionpolicybindings",
    "users", "groups", "userextras", "uids",
}
SHORT_NAMES = {
    "po": "pods", "svc": "services", "ep": "endpoints", "cm": "configmaps", "sa": "serviceaccounts", "pvc": "persistentvolumeclaims",
    "pv": "persistentvolumes", "no": "nodes", "ns": "namespaces", "ev": "events", "deploy": "deployments", "rs": "replicasets",
    "sts": "statefulsets", "ds": "daemonsets", "cj": "cronjobs", "hpa": "horizontalpodautoscalers", "pdb": "poddisruptionbudgets",
    "ing": "ingresses", "netpol": "networkpolicies", "sc": "storageclasses", "crd": "customresourcedefinitions", "csr": "certificatesigningrequests",
}
GROUP_SCOPES = {
    AUTHENTICATED: "every authenticated user and service account",
    UNAUTHENTICATED: "every anonymous request",
    ALL_SERVICE_ACCOUNTS: "every service account in every namespace",
    PRIVILEGED_GROUP: "superusers: allowed before RBAC is consulted",
}


class RbacError(Exception):
    pass


@dataclass(frozen=True)
class Rule:
    verbs: Tuple[str, ...] = ()
    api_groups: Tuple[str, ...] = ()
    resources: Tuple[str, ...] = ()
    resource_names: Tuple[str, ...] = ()
    non_resource_urls: Tuple[str, ...] = ()

    @classmethod
    def from_json(cls, doc: dict) -> "Rule":
        return cls(
            tuple(doc.get("verbs") or ()),
            tuple(doc.get("apiGroups") or ()),
            tuple(doc.get("resources") or ()),
            tuple(doc.get("resourceNames") or ()),
            tuple(doc.get("nonResourceURLs") or ()),
        )

    def to_json(self) -> Dict[str, List[str]]:
        fields = (("apiGroups", self.api_groups), ("resources", self.resources), ("resourceNames", self.resource_names), ("nonResourceURLs", self.non_resource_urls), ("verbs", self.verbs))
        return {key: list(values) for key, values in fields if values}


@dataclass
class Role:
    kind: str
    name: str
    namespace: str
    rules: List[Rule]
    labels: Dict[str, str] = field(default_factory=dict)
    selectors: Optional[List[dict]] = None


@dataclass(frozen=True)
class Subject:
    kind: str
    name: str
    namespace: str = ""

    def label(self) -> str:
        return f"{self.namespace}/{self.name}" if self.kind == "ServiceAccount" else self.name

    def username(self) -> str:
        if self.kind == "ServiceAccount":
            return service_account_user(self.namespace, self.name)
        return self.name if self.kind == "User" else ""

    def to_json(self) -> Dict[str, str]:
        doc = {"kind": self.kind, "name": self.name}
        if self.namespace:
            doc["namespace"] = self.namespace
        return doc


@dataclass
class Binding:
    kind: str
    name: str
    namespace: str
    role_kind: str
    role_name: str
    subjects: List[Subject]

    def ref(self) -> str:
        return f"{self.kind} {self.namespace + '/' if self.namespace else ''}{self.name}"


@dataclass
class Policy:
    roles: Dict[Tuple[str, str], Role]
    cluster_roles: Dict[str, Role]
    cluster_role_bindings: List[Binding]
    role_bindings: Dict[str, List[Binding]]
    effective: Dict[str, List[Rule]]
    warnings: List[str]


@dataclass
class Identity:
    user: str
    groups: List[str]

    def to_json(self) -> dict:
        return {"username": self.user, "groups": list(self.groups)}


@dataclass
class Request:
    verb: str
    namespace: str = ""
    api_group: str = ""
    resource: str = ""
    subresource: str = ""
    name: str = ""
    path: str = ""

    @property
    def is_resource(self) -> bool:
        return not self.path

    @property
    def combined_resource(self) -> str:
        return f"{self.resource}/{self.subresource}" if self.subresource else self.resource

    def describe(self) -> str:
        if not self.is_resource:
            return f"{self.verb} {self.path}"
        target = combine_resource_group(self.combined_resource, self.api_group)
        if self.name:
            target += f" named {self.name}"
        return f"{self.verb} {target} " + (f"in namespace {self.namespace}" if self.namespace else "at cluster scope")

    def to_json(self) -> dict:
        if not self.is_resource:
            return {"verb": self.verb, "path": self.path}
        return {"verb": self.verb, "namespace": self.namespace, "group": self.api_group, "resource": self.resource, "subresource": self.subresource, "name": self.name}


@dataclass
class Visit:
    binding: Binding
    subject: Subject
    rules: List[Rule]
    error: str = ""


@dataclass
class Grant:
    binding: Binding
    subject: Subject
    rule: Rule

    @property
    def source(self) -> str:
        return describe_source(self.binding, self.subject)

    def to_json(self) -> dict:
        return {
            "source": self.source,
            "binding": {"kind": self.binding.kind, "name": self.binding.name, "namespace": self.binding.namespace},
            "role": {"kind": self.binding.role_kind, "name": self.binding.role_name},
            "subject": self.subject.to_json(),
            "rule": self.rule.to_json(),
        }


@dataclass
class Decision:
    allowed: bool
    identity: Identity
    request: Request
    grants: List[Grant]
    errors: List[str]
    visited: int = 0
    privileged: bool = False

    @property
    def reason(self) -> str:
        if self.privileged:
            return ""
        if self.grants:
            return f"RBAC: allowed by {self.grants[0].source}"
        if self.errors:
            return "RBAC: " + aggregate_errors(self.errors)
        return ""

    @property
    def forbidden(self) -> Optional[str]:
        return None if self.allowed else forbidden_message(self.identity, self.request, self.reason)


@dataclass
class AccessRow:
    resource: str
    non_resource_urls: Tuple[str, ...]
    resource_names: Tuple[str, ...]
    verbs: List[str]

    def to_json(self) -> dict:
        return {"resource": self.resource, "non_resource_urls": list(self.non_resource_urls), "resource_names": list(self.resource_names), "verbs": list(self.verbs)}


@dataclass
class WhoRow:
    subject: Subject
    binding: Binding
    rule: Rule

    def to_json(self) -> dict:
        return dict(self.subject.to_json(), username=self.subject.username(), binding=self.binding.ref(), role=f"{self.binding.role_kind} {self.binding.role_name}", scope=group_scope(self.subject), rule=self.rule.to_json())


@dataclass
class BindCheck:
    allowed: bool
    how: str
    binding_kind: str
    namespace: str
    role_kind: str
    role_name: str
    identity: Identity
    via: str = ""
    missing: List[Rule] = field(default_factory=list)
    message: str = ""

    def to_json(self) -> dict:
        return {
            "allowed": self.allowed,
            "how": self.how,
            "binding_kind": self.binding_kind,
            "namespace": self.namespace,
            "role": {"kind": self.role_kind, "name": self.role_name},
            "identity": self.identity.to_json(),
            "via": self.via,
            "missing": [rule.to_json() for rule in self.missing],
            "message": self.message,
        }


def iter_objects(doc: object, kind_hint: str = "") -> Iterator[dict]:
    if isinstance(doc, list):
        for item in doc:
            yield from iter_objects(item, kind_hint)
        return
    if not isinstance(doc, dict):
        raise RbacError(f"expected JSON objects, found a {type(doc).__name__}")
    kind = doc.get("kind") or kind_hint
    if isinstance(doc.get("items"), list):
        yield from iter_objects(doc["items"], kind[:-4] if kind.endswith("List") and kind != "List" else "")
        return
    if kind in OBJECT_KINDS:
        yield dict(doc, kind=kind)


def parse_subject(doc: dict, where: str) -> Subject:
    kind = doc.get("kind", "")
    if kind not in ("User", "Group", "ServiceAccount"):
        raise RbacError(f"{where}: subject kind must be User, Group or ServiceAccount, got {kind!r}")
    return Subject(kind, doc.get("name", ""), doc.get("namespace") or "")


def load_policy(docs: Sequence[object], aggregate: bool = True) -> Policy:
    roles: Dict[Tuple[str, str], Role] = {}
    cluster_roles: Dict[str, Role] = {}
    bindings: Dict[Tuple[str, str, str], Binding] = {}
    for doc in docs:
        for obj in iter_objects(doc):
            kind = obj["kind"]
            meta = obj.get("metadata") or {}
            name = meta.get("name")
            if not name:
                raise RbacError(f"a {kind} without metadata.name")
            namespace = "" if kind.startswith("Cluster") else meta.get("namespace") or "default"
            where = f"{kind} {namespace + '/' if namespace else ''}{name}"
            if kind in ("Role", "ClusterRole"):
                aggregation = obj.get("aggregationRule") if kind == "ClusterRole" else None
                selectors = None if aggregation is None else list(aggregation.get("clusterRoleSelectors") or [])
                role = Role(kind, name, namespace, [Rule.from_json(rule) for rule in obj.get("rules") or []], dict(meta.get("labels") or {}), selectors)
                if kind == "Role":
                    roles[(namespace, name)] = role
                else:
                    cluster_roles[name] = role
                continue
            ref = obj.get("roleRef") or {}
            if ref.get("kind") not in ("Role", "ClusterRole") or not ref.get("name"):
                raise RbacError(f"{where}: roleRef needs kind Role or ClusterRole and a name")
            subjects = [parse_subject(subject, where) for subject in obj.get("subjects") or []]
            bindings[(kind, namespace, name)] = Binding(kind, name, namespace, ref["kind"], ref["name"], subjects)
    if aggregate:
        effective, warnings = aggregate_cluster_roles(cluster_roles)
    else:
        effective, warnings = {name: list(role.rules) for name, role in cluster_roles.items()}, []
    cluster_role_bindings = sorted((b for b in bindings.values() if b.kind == "ClusterRoleBinding"), key=lambda b: b.name)
    role_bindings: Dict[str, List[Binding]] = {}
    for binding in sorted((b for b in bindings.values() if b.kind == "RoleBinding"), key=lambda b: (b.namespace, b.name)):
        role_bindings.setdefault(binding.namespace, []).append(binding)
    return Policy(roles, cluster_roles, cluster_role_bindings, role_bindings, effective, warnings)


def read_documents(paths: Sequence[str], stdin: Optional[TextIO] = None) -> List[object]:
    docs: List[object] = []
    for path in paths:
        try:
            if path == "-":
                text = (stdin or sys.stdin).read()
            else:
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
        except FileNotFoundError:
            raise RbacError(f"{path} not found; dump the cluster's RBAC first: {DUMP_COMMAND}")
        except OSError as exc:
            raise RbacError(f"cannot read {path}: {exc.strerror}")
        try:
            docs.append(json.loads(text))
        except json.JSONDecodeError as exc:
            raise RbacError(f"{path}: not JSON ({exc.msg} at line {exc.lineno}); ask kubectl for -o json, canhaz does not read YAML")
    return docs


def selector_matches(selector: dict, labels: Dict[str, str]) -> bool:
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expression in selector.get("matchExpressions") or []:
        key, operator, values = expression.get("key"), expression.get("operator"), expression.get("values") or []
        if operator == "In":
            ok = key in labels and labels[key] in values
        elif operator == "NotIn":
            ok = key not in labels or labels[key] not in values
        elif operator == "Exists":
            ok = key in labels
        elif operator == "DoesNotExist":
            ok = key not in labels
        else:
            raise RbacError(f"unknown label selector operator {operator!r}")
        if not ok:
            return False
    return True


def aggregate_cluster_roles(cluster_roles: Dict[str, Role]) -> Tuple[Dict[str, List[Rule]], List[str]]:
    effective = {name: [] if role.selectors is not None else list(role.rules) for name, role in cluster_roles.items()}
    aggregated = sorted(name for name, role in cluster_roles.items() if role.selectors is not None)
    for _ in range(len(cluster_roles) + 1):
        changed = False
        for name in aggregated:
            rules: List[Rule] = []
            for selector in cluster_roles[name].selectors or []:
                for other in sorted(cluster_roles):
                    if other == name or not selector_matches(selector, cluster_roles[other].labels):
                        continue
                    for rule in effective[other]:
                        if rule not in rules:
                            rules.append(rule)
            if set(rules) != set(effective[name]):
                changed = True
            effective[name] = rules
        if not changed:
            break
    warnings = []
    for name in aggregated:
        stored, computed = set(cluster_roles[name].rules), set(effective[name])
        if stored != computed:
            warnings.append(
                f"clusterrole {name}: stored rules differ from its aggregationRule ({len(computed - stored)} not copied yet, {len(stored - computed)} stale); "
                "canhaz uses the aggregation, which is what the controller will write"
            )
    return effective, warnings


def service_account_user(namespace: str, name: str) -> str:
    return f"{SA_PREFIX}{namespace}:{name}"


def split_service_account(user: str) -> Optional[Tuple[str, str]]:
    if not user.startswith(SA_PREFIX):
        return None
    parts = user[len(SA_PREFIX):].split(":")
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


def identity_for(user: str, groups: Sequence[str] = (), implicit: bool = True) -> Identity:
    wanted = list(dict.fromkeys(groups))
    if not implicit:
        return Identity(user, wanted)
    account = split_service_account(user)
    if account and not wanted:
        wanted = [ALL_SERVICE_ACCOUNTS, f"{ALL_SERVICE_ACCOUNTS}:{account[0]}"]
    if user != ANONYMOUS and AUTHENTICATED not in wanted and UNAUTHENTICATED not in wanted:
        wanted.append(AUTHENTICATED)
    return Identity(user, wanted)


def resolve_resource(text: str) -> Tuple[str, str, Optional[str]]:
    lowered = text.lower()
    if lowered == "*":
        return "*", "", None
    resource, dot, group = lowered.partition(".")
    if dot:
        return resource, group, None
    resource = SHORT_NAMES.get(resource, resource)
    candidates = [resource, resource + "s", resource + "es"] + ([resource[:-1] + "ies"] if resource.endswith("y") else [])
    for candidate in candidates:
        if candidate in GROUP_OF:
            return candidate, GROUP_OF[candidate], None
    return resource, "", f"canhaz does not know which API group serves {resource!r}; assuming the core group (write RESOURCE.GROUP to be explicit)"


def parse_request(verb: str, target: str, namespace: str = "", subresource: str = "") -> Tuple[Request, List[str]]:
    if target.startswith("/"):
        if subresource:
            raise RbacError("--subresource can not be used with a non-resource URL")
        return Request(verb=verb, path=target), []
    kind, _, name = target.partition("/")
    resource, group, warning = resolve_resource(kind)
    warnings = [warning] if warning else []
    if namespace and resource in CLUSTER_SCOPED:
        warnings.append(f"{resource} is cluster-scoped: a real request carries no namespace, so ask with -A to see what the API server decides")
    return Request(verb, namespace, group, resource, subresource, name), warnings


def verb_matches(rule: Rule, verb: str) -> bool:
    return "*" in rule.verbs or verb in rule.verbs


def group_matches(rule: Rule, group: str) -> bool:
    return "*" in rule.api_groups or group in rule.api_groups


def resource_matches(rule: Rule, combined: str, subresource: str) -> bool:
    for resource in rule.resources:
        if resource == "*" or resource == combined:
            return True
        if subresource and resource == "*/" + subresource:
            return True
    return False


def name_matches(rule: Rule, name: str) -> bool:
    return not rule.resource_names or name in rule.resource_names


def url_matches(rule: Rule, path: str) -> bool:
    for url in rule.non_resource_urls:
        if url == "*" or url == path:
            return True
        if url.endswith("*") and path.startswith(url.rstrip("*")):
            return True
    return False


def rule_allows(rule: Rule, request: Request) -> bool:
    if request.is_resource:
        return (
            verb_matches(rule, request.verb)
            and group_matches(rule, request.api_group)
            and resource_matches(rule, request.combined_resource, request.subresource)
            and name_matches(rule, request.name)
        )
    return verb_matches(rule, request.verb) and url_matches(rule, request.path)


def applies_to(subject: Subject, identity: Identity, namespace: str) -> bool:
    if subject.kind == "User":
        return identity.user == subject.name
    if subject.kind == "Group":
        return subject.name in identity.groups
    if subject.kind == "ServiceAccount":
        account_namespace = subject.namespace or namespace
        return bool(account_namespace) and identity.user == service_account_user(account_namespace, subject.name)
    return False


def bindings_for(policy: Policy, namespace: str) -> Iterator[Tuple[Binding, str]]:
    for binding in policy.cluster_role_bindings:
        yield binding, ""
    if namespace:
        for binding in policy.role_bindings.get(namespace, []):
            yield binding, namespace


def role_rules(policy: Policy, binding: Binding, namespace: str) -> Tuple[List[Rule], str]:
    if binding.role_kind == "ClusterRole":
        if binding.role_name in policy.effective:
            return policy.effective[binding.role_name], ""
        return [], f'clusterrole.rbac.authorization.k8s.io "{binding.role_name}" not found'
    role = policy.roles.get((namespace, binding.role_name)) if namespace else None
    if role is None:
        return [], f'role.rbac.authorization.k8s.io "{binding.role_name}" not found'
    return role.rules, ""


def visit(policy: Policy, identity: Identity, namespace: str) -> Iterator[Visit]:
    for binding, scope in bindings_for(policy, namespace):
        subject = next((s for s in binding.subjects if applies_to(s, identity, scope)), None)
        if subject is None:
            continue
        rules, error = role_rules(policy, binding, scope)
        yield Visit(binding, subject, rules, error)


def authorize(policy: Policy, identity: Identity, request: Request) -> Decision:
    if PRIVILEGED_GROUP in identity.groups:
        return Decision(True, identity, request, [], [], privileged=True)
    namespace = request.namespace if request.is_resource else ""
    grants: List[Grant] = []
    errors: List[str] = []
    visited = 0
    for seen in visit(policy, identity, namespace):
        visited += 1
        if seen.error:
            errors.append(seen.error)
            continue
        rule = next((r for r in seen.rules if rule_allows(r, request)), None)
        if rule is not None:
            grants.append(Grant(seen.binding, seen.subject, rule))
    return Decision(bool(grants), identity, request, grants, errors, visited)


def describe_subject(subject: Subject, binding_namespace: str) -> str:
    if subject.kind == "ServiceAccount":
        return f"ServiceAccount {quote(subject.name + '/' + (subject.namespace or binding_namespace))}"
    return f"{subject.kind} {quote(subject.name)}"


def describe_source(binding: Binding, subject: Subject) -> str:
    if binding.kind == "ClusterRoleBinding":
        return f"ClusterRoleBinding {quote(binding.name)} of {binding.role_kind} {quote(binding.role_name)} to {describe_subject(subject, '')}"
    return f"RoleBinding {quote(binding.name + '/' + binding.namespace)} of {binding.role_kind} {quote(binding.role_name)} to {describe_subject(subject, binding.namespace)}"


def aggregate_errors(errors: Sequence[str]) -> str:
    unique = list(dict.fromkeys(errors))
    return unique[0] if len(unique) == 1 else "[" + ", ".join(unique) + "]"


def forbidden_message(identity: Identity, request: Request, reason: str = "") -> str:
    user = quote(identity.user)
    if not request.is_resource:
        message = f"User {user} cannot {request.verb} path {quote(request.path)}"
    elif request.namespace:
        message = f"User {user} cannot {request.verb} resource {quote(request.combined_resource)} in API group {quote(request.api_group)} in the namespace {quote(request.namespace)}"
    else:
        message = f"User {user} cannot {request.verb} resource {quote(request.combined_resource)} in API group {quote(request.api_group)} at the cluster scope"
    if reason:
        message += f": {reason}"
    if not request.is_resource:
        return f"forbidden: {message}"
    qualified = request.resource + (f".{request.api_group}" if request.api_group else "")
    if request.name:
        return f"{qualified} {quote(request.name)} is forbidden: {message}"
    return f"{qualified} is forbidden: {message}"


def rules_for(policy: Policy, identity: Identity, namespace: str) -> Tuple[List[Rule], List[str]]:
    rules: List[Rule] = []
    errors: List[str] = []
    for seen in visit(policy, identity, namespace):
        if seen.error:
            errors.append(seen.error)
        else:
            rules.extend(seen.rules)
    return rules, errors


def breakdown(rule: Rule) -> List[Rule]:
    pieces: List[Rule] = []
    for group in rule.api_groups:
        for resource in rule.resources:
            for verb in rule.verbs:
                if rule.resource_names:
                    pieces.extend(Rule((verb,), (group,), (resource,), (name,)) for name in rule.resource_names)
                else:
                    pieces.append(Rule((verb,), (group,), (resource,)))
    for url in rule.non_resource_urls:
        pieces.extend(Rule((verb,), non_resource_urls=(url,)) for verb in rule.verbs)
    return pieces


def compact(rules: Sequence[Rule]) -> List[Rule]:
    simple: Dict[Tuple[str, str, Tuple[str, ...]], List[str]] = {}
    others: List[Rule] = []
    for rule in rules:
        if len(rule.api_groups) == 1 and len(rule.resources) == 1 and len(rule.resource_names) <= 1 and not rule.non_resource_urls:
            verbs = simple.setdefault((rule.api_groups[0], rule.resources[0], rule.resource_names), [])
            for verb in rule.verbs:
                if verb not in verbs:
                    verbs.append(verb)
        else:
            others.append(rule)
    return others + [Rule(tuple(verbs), (group,), (resource,), names) for (group, resource, names), verbs in simple.items()]


def combine_resource_group(resource: str, group: str) -> str:
    base, _, sub = resource.partition("/")
    combined = base + (f".{group}" if group else "")
    return combined + (f"/{sub}" if sub else "")


def access_list(policy: Policy, identity: Identity, namespace: str) -> Tuple[List[AccessRow], List[str]]:
    rules, errors = rules_for(policy, identity, namespace)
    rows: Dict[Tuple[str, Tuple[str, ...], Tuple[str, ...]], List[str]] = {}
    for rule in compact([piece for r in rules for piece in breakdown(r)]):
        resource = combine_resource_group(rule.resources[0], rule.api_groups[0]) if rule.resources else ""
        verbs = rows.setdefault((resource, rule.non_resource_urls, rule.resource_names), [])
        for verb in rule.verbs:
            if verb not in verbs:
                verbs.append(verb)
    ordered = sorted(rows.items(), key=lambda item: (item[0][0] == "", item[0]))
    return [AccessRow(key[0], key[1], key[2], verbs) for key, verbs in ordered], errors


def group_scope(subject: Subject) -> str:
    if subject.kind != "Group":
        return ""
    if subject.name in GROUP_SCOPES:
        return GROUP_SCOPES[subject.name]
    if subject.name.startswith(ALL_SERVICE_ACCOUNTS + ":"):
        return f"every service account in {subject.name.split(':', 2)[2]}"
    return ""


def who_can(policy: Policy, request: Request) -> Tuple[List[WhoRow], List[str]]:
    namespace = request.namespace if request.is_resource else ""
    rows: List[WhoRow] = []
    errors: List[str] = []
    for binding, scope in bindings_for(policy, namespace):
        rules, error = role_rules(policy, binding, scope)
        if error:
            errors.append(f"{binding.ref()}: {error}")
            continue
        rule = next((r for r in rules if rule_allows(r, request)), None)
        if rule is None:
            continue
        for subject in binding.subjects:
            if subject.kind == "ServiceAccount":
                account_namespace = subject.namespace or scope
                if not account_namespace:
                    continue
                subject = Subject(subject.kind, subject.name, account_namespace)
            rows.append(WhoRow(subject, binding, rule))
    return rows, errors


def url_covers(owner: str, path: str) -> bool:
    return owner == path or (owner.endswith("*") and path.startswith(owner.rstrip("*")))


def resource_covers_all(owned: Sequence[str], wanted: Sequence[str]) -> bool:
    if "*" in owned or all(resource in owned for resource in wanted):
        return True
    for path in wanted:
        if path in owned:
            continue
        if "/" not in path or "*/" + path.split("/", 1)[1] not in owned:
            return False
    return True


def rule_covers(owner: Rule, wanted: Rule) -> bool:
    verbs = "*" in owner.verbs or all(verb in owner.verbs for verb in wanted.verbs)
    groups = "*" in owner.api_groups or all(group in owner.api_groups for group in wanted.api_groups)
    resources = resource_covers_all(owner.resources, wanted.resources)
    urls = all(any(url_covers(mine, path) for mine in owner.non_resource_urls) for path in wanted.non_resource_urls)
    if wanted.resource_names:
        names = not owner.resource_names or all(name in owner.resource_names for name in wanted.resource_names)
    else:
        names = not owner.resource_names
    return verbs and groups and resources and urls and names


def covers(held: Sequence[Rule], granted: Sequence[Rule]) -> Tuple[bool, List[Rule]]:
    missing = [piece for rule in granted for piece in breakdown(rule) if not any(rule_covers(owner, piece) for owner in held)]
    return not missing, missing


def quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def go_quoted_list(values: Sequence[str]) -> str:
    return "[" + " ".join(quote(value) for value in values) + "]"


def go_list(values: Sequence[str]) -> str:
    return "[" + " ".join(values) + "]"


def compact_string(rule: Rule) -> str:
    fields = (("APIGroups", rule.api_groups), ("Resources", rule.resources), ("NonResourceURLs", rule.non_resource_urls), ("ResourceNames", rule.resource_names), ("Verbs", rule.verbs))
    return "{" + ", ".join(f"{label}:{go_quoted_list(values)}" for label, values in fields if values) + "}"


def escalation_message(identity: Identity, missing: Sequence[Rule]) -> str:
    lines = sorted({compact_string(rule) for rule in missing})
    return f"user {quote(identity.user)} (groups={go_quoted_list(identity.groups)}) is attempting to grant RBAC permissions not currently held:\n" + "\n".join(lines)


def can_bind(policy: Policy, identity: Identity, role_kind: str, role_name: str, namespace: str) -> BindCheck:
    binding_kind = "RoleBinding" if namespace else "ClusterRoleBinding"
    if not namespace and role_kind == "Role":
        raise RbacError("a ClusterRoleBinding can only reference a ClusterRole")

    def result(allowed: bool, how: str, via: str = "", missing: Sequence[Rule] = (), message: str = "") -> BindCheck:
        return BindCheck(allowed, how, binding_kind, namespace, role_kind, role_name, identity, via, list(missing), message)

    create = authorize(policy, identity, Request("create", namespace, RBAC_GROUP, binding_kind.lower() + "s"))
    if not create.allowed:
        return result(False, "forbidden", message=create.forbidden or "")
    if PRIVILEGED_GROUP in identity.groups:
        return result(True, "privileged", via=f"member of {PRIVILEGED_GROUP}: the escalation check is skipped")
    bind = authorize(policy, identity, Request("bind", namespace, RBAC_GROUP, "clusterroles" if role_kind == "ClusterRole" else "roles", name=role_name))
    if bind.allowed:
        return result(True, "bind", via=f"holds bind on {role_kind.lower()}s/{role_name}: {bind.reason}")
    granted, error = role_rules(policy, Binding(binding_kind, "", namespace, role_kind, role_name, []), namespace)
    if error:
        return result(False, "not-found", message=error)
    held, _ = rules_for(policy, identity, namespace)
    covered, missing = covers(held, granted)
    if covered:
        return result(True, "covers", via=f"already holds every permission {role_kind} {role_name} grants")
    missing = compact(missing)
    return result(False, "escalation", missing=missing, message=escalation_message(identity, missing))


def render_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    table = [list(header)] + [list(row) for row in rows]
    widths = [max(len(row[index]) for row in table) for index in range(len(header))]
    return "\n".join("   ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip() for row in table)


def render_decision(decision: Decision) -> str:
    lines = ["yes" if decision.allowed else "no"]
    who = f"user {quote(decision.identity.user)} in groups {', '.join(decision.identity.groups) or '(none)'}"
    if decision.privileged:
        lines.append(f"  {PRIVILEGED_GROUP} is allowed everything before RBAC is consulted")
    elif decision.allowed:
        lines.append(f"  {decision.reason}")
        lines.append(f"  rule {compact_string(decision.grants[0].rule)}")
        lines.extend(f"  also allowed by {grant.source}" for grant in decision.grants[1:])
    else:
        lines.append(f"  no rule matched {decision.request.describe()} for {who}")
        lines.append(f"  RBAC is deny-by-default and additive: {decision.visited} binding(s) apply to this identity here and none grants it")
        if decision.errors:
            lines.append(f"  {decision.reason}")
        lines.append(f"  the API server answers 403: {decision.forbidden}")
    return "\n".join(lines)


def render_who(rows: Sequence[WhoRow]) -> str:
    body = [[row.subject.kind, row.subject.label(), row.binding.ref(), f"{row.binding.role_kind} {row.binding.role_name}", group_scope(row.subject)] for row in rows]
    return render_table(("KIND", "SUBJECT", "VIA", "ROLE", "MEANS"), body)


def render_bind(check: BindCheck) -> str:
    lines = ["yes" if check.allowed else "no"]
    target = f"a {check.binding_kind}" + (f" in {check.namespace}" if check.namespace else "") + f" to {check.role_kind} {check.role_name}"
    if check.allowed:
        lines.append(f"  {target}: {check.via}")
    elif check.how == "escalation":
        lines.append(f"  {target} is refused by the escalation check:")
        lines.extend(f"  {line}" for line in check.message.splitlines())
        lines.append(f"  either grant bind on {check.role_kind.lower()}s/{check.role_name} or bind a smaller role this identity already covers")
    else:
        lines.append(f"  {target}: {check.message}")
    return "\n".join(lines)


def warn(message: str) -> None:
    print(f"canhaz: warning: {message}", file=sys.stderr)


def identity_from_args(args: argparse.Namespace) -> Identity:
    if not args.as_user:
        if args.as_group:
            raise RbacError("--as-group needs --as: groups belong to a user")
        raise RbacError("say who is asking with --as USER (a service account is --as system:serviceaccount:NAMESPACE:NAME); canhaz has no credentials of its own")
    return identity_for(args.as_user, args.as_group, implicit=not args.no_default_groups)


def build_parser() -> argparse.ArgumentParser:
    data = argparse.ArgumentParser(add_help=False)
    data.add_argument("-f", "--filename", action="append", metavar="FILE", help=f"RBAC dump as JSON; repeat to layer proposed objects on top (default rbac.json, - for stdin). Make one with: {DUMP_COMMAND}")
    data.add_argument("--no-aggregate", action="store_true", help="use stored ClusterRole rules as they are instead of recomputing aggregationRule")
    output = argparse.ArgumentParser(add_help=False)
    output.add_argument("--json", action="store_true", help="machine-readable output")
    who = argparse.ArgumentParser(add_help=False)
    who.add_argument("--as", dest="as_user", metavar="USER", help="the user to evaluate, e.g. system:serviceaccount:NS:NAME")
    who.add_argument("--as-group", action="append", default=[], metavar="GROUP", help="a group of --as (repeatable)")
    who.add_argument("--no-default-groups", action="store_true", help="use exactly these groups, like a SubjectAccessReview, instead of adding the ones impersonation adds")
    scope = argparse.ArgumentParser(add_help=False)
    scope.add_argument("-n", "--namespace", default="default", help="namespace of the request (default: default, like kubectl)")
    scope.add_argument("-A", "--all-namespaces", action="store_true", help="ask at cluster scope, where only ClusterRoleBindings count")

    parser = argparse.ArgumentParser(prog="canhaz", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    can = sub.add_parser("can-i", parents=[data, output, who, scope], help="answer one question, or --list everything")
    can.add_argument("verb", nargs="?")
    can.add_argument("target", nargs="?", help="RESOURCE[.GROUP][/NAME], or a non-resource URL such as /healthz")
    can.add_argument("--subresource", default="", help="log, exec, scale, status, ...")
    can.add_argument("--list", action="store_true", help="every permission in the namespace, like kubectl auth can-i --list")
    whocan = sub.add_parser("who-can", parents=[data, output, scope], help="the subjects allowed to do something")
    whocan.add_argument("verb")
    whocan.add_argument("target", help="RESOURCE[.GROUP][/NAME], or a non-resource URL")
    whocan.add_argument("--subresource", default="")
    bind = sub.add_parser("can-bind", parents=[data, output, who], help="would creating a binding to this role pass the escalation check")
    role = bind.add_mutually_exclusive_group(required=True)
    role.add_argument("--role", help="a Role in the binding's namespace")
    role.add_argument("--clusterrole", help="a ClusterRole")
    bind.add_argument("-n", "--namespace", default="default", help="namespace of the RoleBinding (default: default)")
    bind.add_argument("--cluster", action="store_true", help="a ClusterRoleBinding instead of a RoleBinding")
    sub.add_parser("whoami", parents=[output, who], help="the user and groups canhaz evaluates, like kubectl auth whoami")
    return parser


def run_can_i(policy: Policy, args: argparse.Namespace) -> int:
    identity = identity_from_args(args)
    namespace = "" if args.all_namespaces else args.namespace
    if args.list:
        rows, errors = access_list(policy, identity, namespace)
        for error in errors:
            warn(f"the list may be incomplete: RBAC: {error}")
        if args.json:
            print(json.dumps({"identity": identity.to_json(), "namespace": namespace, "rules": [row.to_json() for row in rows], "errors": errors}, indent=2))
        else:
            print(render_table(LIST_HEADER, [[row.resource, go_list(row.non_resource_urls), go_list(row.resource_names), go_list(row.verbs)] for row in rows]))
        return 0
    if not args.verb or not args.target:
        raise RbacError("can-i needs VERB and RESOURCE (or --list)")
    request, warnings = parse_request(args.verb, args.target, namespace, args.subresource)
    for warning in warnings:
        warn(warning)
    decision = authorize(policy, identity, request)
    if args.json:
        doc = {
            "allowed": decision.allowed,
            "reason": decision.reason,
            "identity": identity.to_json(),
            "request": request.to_json(),
            "grants": [grant.to_json() for grant in decision.grants],
            "errors": decision.errors,
            "privileged": decision.privileged,
            "forbidden": decision.forbidden,
        }
        print(json.dumps(doc, indent=2))
    else:
        print(render_decision(decision))
    return 0 if decision.allowed else 1


def run_who_can(policy: Policy, args: argparse.Namespace) -> int:
    request, warnings = parse_request(args.verb, args.target, "" if args.all_namespaces else args.namespace, args.subresource)
    for warning in warnings:
        warn(warning)
    rows, errors = who_can(policy, request)
    for error in errors:
        warn(f"skipped {error}")
    if args.json:
        print(json.dumps({"request": request.to_json(), "subjects": [row.to_json() for row in rows], "privileged_groups": [PRIVILEGED_GROUP], "errors": errors}, indent=2))
    else:
        print(render_who(rows) if rows else f"nobody is bound to a rule that allows {request.describe()}")
        if not any(row.subject == Subject("Group", PRIVILEGED_GROUP) for row in rows):
            print(f"(and Group {PRIVILEGED_GROUP}, which is allowed everything before RBAC is consulted)")
    return 0 if rows else 1


def run_can_bind(policy: Policy, args: argparse.Namespace) -> int:
    identity = identity_from_args(args)
    role_kind, role_name = ("Role", args.role) if args.role else ("ClusterRole", args.clusterrole)
    check = can_bind(policy, identity, role_kind, role_name, "" if args.cluster else args.namespace)
    print(json.dumps(check.to_json(), indent=2) if args.json else render_bind(check))
    return 0 if check.allowed else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "whoami":
            identity = identity_from_args(args)
            if args.json:
                print(json.dumps(identity.to_json(), indent=2))
            else:
                print(render_table(("ATTRIBUTE", "VALUE"), [["Username", identity.user], ["Groups", go_list(identity.groups)]]))
            return 0
        policy = load_policy(read_documents(args.filename or ["rbac.json"]), aggregate=not args.no_aggregate)
        for warning in policy.warnings:
            warn(warning)
        if args.command == "can-i":
            return run_can_i(policy, args)
        if args.command == "who-can":
            return run_who_can(policy, args)
        return run_can_bind(policy, args)
    except RbacError as exc:
        print(f"canhaz: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
