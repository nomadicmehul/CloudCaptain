from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
WRITE_ACTIONS = ("create", "update")
PUBLIC_ACLS = ("public-read", "public-read-write", "authenticated-read")
STRONG_SSE = ("AES256", "aws:kms", "aws:kms:dsse")
WORLD = ("0.0.0.0/0", "::/0")
INSTANCE_KEY = re.compile(r"\[[^\]]*\]")
MODULE_PREFIX = re.compile(r"^((?:module\.[A-Za-z0-9_-]+(?:\[[^\]]*\])?\.)*)")
ENCRYPTED_FLAGS = {
    "aws_ebs_volume": "encrypted",
    "aws_efs_file_system": "encrypted",
    "aws_db_instance": "storage_encrypted",
    "aws_rds_cluster": "storage_encrypted",
}
CIDR_ATTRIBUTES = {
    "aws_vpc": "cidr_block",
    "aws_subnet": "cidr_block",
    "aws_vpc_ipv4_cidr_block_association": "cidr_block",
    "google_compute_subnetwork": "ip_cidr_range",
    "azurerm_virtual_network": "address_space",
    "azurerm_subnet": "address_prefixes",
}


class PlanError(Exception):
    pass


@dataclass
class Violation:
    rule: str
    address: str
    message: str


@dataclass
class Resource:
    address: str
    config_address: str
    module: str
    type: str
    name: str
    index: str
    after: Dict[str, Any]
    after_unknown: Dict[str, Any]


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    with open(path or os.path.join(HERE, "landing-zone.json"), encoding="utf-8") as handle:
        return json.load(handle)


def module_of(address: str) -> str:
    match = MODULE_PREFIX.match(address)
    return match.group(1)[:-1] if match and match.group(1) else ""


def index_of(address: str) -> str:
    match = re.search(r"(\[[^\]]*\])$", address)
    return match.group(1) if match else ""


def resources(plan: Dict[str, Any]) -> List[Resource]:
    found = []
    for change in plan.get("resource_changes") or []:
        actions = (change.get("change") or {}).get("actions") or []
        if change.get("mode", "managed") != "managed" or not set(actions) & set(WRITE_ACTIONS):
            continue
        address = str(change.get("address", ""))
        found.append(Resource(
            address=address,
            config_address=INSTANCE_KEY.sub("", address),
            module=module_of(address),
            type=str(change.get("type", "")),
            name=str(change.get("name", "")),
            index=index_of(address),
            after=(change.get("change") or {}).get("after") or {},
            after_unknown=(change.get("change") or {}).get("after_unknown") or {},
        ))
    return found


def config_references(plan: Dict[str, Any]) -> Dict[str, Dict[str, List[str]]]:
    found: Dict[str, Dict[str, List[str]]] = {}

    def visit(module: Dict[str, Any], prefix: str) -> None:
        for item in module.get("resources") or []:
            refs = {key: list((value or {}).get("references") or []) for key, value in (item.get("expressions") or {}).items() if isinstance(value, dict)}
            found[prefix + str(item.get("address", ""))] = refs
        for name, call in sorted((module.get("module_calls") or {}).items()):
            visit((call or {}).get("module") or {}, f"{prefix}module.{name}.")

    visit(((plan.get("configuration") or {}).get("root_module")) or {}, "")
    return found


def companions(bucket: Resource, kind: str, everything: List[Resource], refs: Dict[str, Dict[str, List[str]]]) -> List[Resource]:
    local_name = f"aws_s3_bucket.{bucket.name}"
    by_reference = [
        r for r in everything
        if r.type == kind and r.module == bucket.module
        and any(ref == local_name or ref.startswith(local_name + ".") for ref in refs.get(r.config_address, {}).get("bucket", []))
    ]
    if by_reference:
        same_index = [r for r in by_reference if r.index == bucket.index]
        return same_index or by_reference
    name = bucket.after.get("bucket")
    return [r for r in everything if r.type == kind and name and r.after.get("bucket") == name]


def tags_of(resource: Resource) -> Optional[Dict[str, Any]]:
    if "tags_all" in resource.after and isinstance(resource.after.get("tags_all"), dict):
        return resource.after["tags_all"]
    if "tags" in resource.after:
        return resource.after.get("tags") or {}
    if "labels" in resource.after and resource.type.startswith("google_"):
        return resource.after.get("labels") or {}
    return None


def check_required_tags(everything: List[Resource], config: Dict[str, Any], refs: Dict[str, Dict[str, List[str]]]) -> Iterable[Violation]:
    required = list(config["required_tags"])
    for resource in everything:
        if resource.after_unknown.get("tags_all") is True and not resource.after.get("tags"):
            continue
        tags = tags_of(resource)
        if tags is None:
            continue
        missing = [key for key in required if not str(tags.get(key) or "").strip()]
        if missing:
            yield Violation("required-tags", resource.address, f"missing tag(s) {', '.join(missing)}: every taggable resource carries {', '.join(required)}")


def check_public_s3(everything: List[Resource], config: Dict[str, Any], refs: Dict[str, Dict[str, List[str]]]) -> Iterable[Violation]:
    for bucket in (r for r in everything if r.type == "aws_s3_bucket"):
        if str(bucket.after.get("acl") or "") in PUBLIC_ACLS:
            yield Violation("no-public-s3", bucket.address, f"bucket ACL {bucket.after['acl']} makes objects public")
            continue
        blocks = companions(bucket, "aws_s3_bucket_public_access_block", everything, refs)
        if not blocks:
            yield Violation("no-public-s3", bucket.address, "no aws_s3_bucket_public_access_block for this bucket: add one with all four settings true")
            continue
        flags = ("block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets")
        open_flags = [flag for block in blocks for flag in flags if block.after.get(flag) is not True]
        if open_flags:
            yield Violation("no-public-s3", bucket.address, f"public access block leaves {', '.join(sorted(set(open_flags)))} off")
            continue
        acls = companions(bucket, "aws_s3_bucket_acl", everything, refs)
        public = [a for a in acls if str(a.after.get("acl") or "") in PUBLIC_ACLS]
        if public:
            yield Violation("no-public-s3", bucket.address, f"{public[0].address} sets acl {public[0].after.get('acl')}")


def check_open_ingress(everything: List[Resource], config: Dict[str, Any], refs: Dict[str, Dict[str, List[str]]]) -> Iterable[Violation]:
    allowed = config["public_ingress"]
    ports = [int(p) for p in allowed["ports"]]
    tag_key, tag_value = next(iter(allowed["tag"].items()))

    def permitted(protocol: Any, low: Any, high: Any, tags: Dict[str, Any]) -> bool:
        return str(protocol) in (allowed["protocol"], "6") and low == high and low in ports and str(tags.get(tag_key) or "") == tag_value

    for resource in everything:
        tags = resource.after.get("tags") or {}
        if resource.type == "aws_security_group":
            for rule in resource.after.get("ingress") or []:
                sources = list(rule.get("cidr_blocks") or []) + list(rule.get("ipv6_cidr_blocks") or [])
                if any(s in WORLD for s in sources) and not permitted(rule.get("protocol"), rule.get("from_port"), rule.get("to_port"), tags):
                    yield Violation("no-open-ingress", resource.address, f"ingress {rule.get('protocol')} {rule.get('from_port')}-{rule.get('to_port')} from the internet; only {allowed['protocol']} {ports} on a group tagged {tag_key}={tag_value} may be public")
        elif resource.type == "aws_vpc_security_group_ingress_rule":
            if resource.after.get("cidr_ipv4") in WORLD or resource.after.get("cidr_ipv6") in WORLD:
                if not permitted(resource.after.get("ip_protocol"), resource.after.get("from_port"), resource.after.get("to_port"), tags):
                    yield Violation("no-open-ingress", resource.address, "ingress rule open to the internet outside the load-balancer exception")
        elif resource.type == "aws_security_group_rule" and resource.after.get("type") == "ingress":
            sources = list(resource.after.get("cidr_blocks") or []) + list(resource.after.get("ipv6_cidr_blocks") or [])
            if any(s in WORLD for s in sources):
                yield Violation("no-open-ingress", resource.address, "aws_security_group_rule open to the internet: use an inline rule on a group tagged for load balancers")


def check_encryption(everything: List[Resource], config: Dict[str, Any], refs: Dict[str, Dict[str, List[str]]]) -> Iterable[Violation]:
    for resource in everything:
        if resource.type == "aws_s3_bucket":
            sse = companions(resource, "aws_s3_bucket_server_side_encryption_configuration", everything, refs)
            algorithms = [
                d.get("sse_algorithm")
                for s in sse for rule in s.after.get("rule") or [] for d in rule.get("apply_server_side_encryption_by_default") or []
            ]
            if not algorithms or any(a not in STRONG_SSE for a in algorithms):
                yield Violation("encryption", resource.address, "no aws_s3_bucket_server_side_encryption_configuration with AES256 or aws:kms for this bucket")
        elif resource.type in ENCRYPTED_FLAGS and resource.after.get(ENCRYPTED_FLAGS[resource.type]) is not True:
            yield Violation("encryption", resource.address, f"{ENCRYPTED_FLAGS[resource.type]} must be true")


def check_cidr_allocation(everything: List[Resource], config: Dict[str, Any], refs: Dict[str, Dict[str, List[str]]]) -> Iterable[Violation]:
    allocation = ipaddress.ip_network(config["allocation"])
    for resource in everything:
        attribute = CIDR_ATTRIBUTES.get(resource.type)
        if not attribute or resource.after_unknown.get(attribute) is True:
            continue
        values = resource.after.get(attribute)
        for value in values if isinstance(values, list) else [values]:
            if not value:
                continue
            try:
                network = ipaddress.ip_network(str(value), strict=False)
            except ValueError:
                yield Violation("cidr-allocation", resource.address, f"{attribute} {value!r} is not a CIDR range")
                continue
            if network.version != allocation.version or not network.subnet_of(allocation):  # type: ignore[arg-type]
                yield Violation("cidr-allocation", resource.address, f"{attribute} {value} is outside the landing zone allocation {allocation}")


RULES = (check_required_tags, check_public_s3, check_open_ingress, check_encryption, check_cidr_allocation)


def evaluate(plan: Any, config: Optional[Dict[str, Any]] = None) -> List[Violation]:
    if not isinstance(plan, dict) or "resource_changes" not in plan and "planned_values" not in plan:
        raise PlanError("not a plan: expected the output of `tofu show -json PLANFILE` with resource_changes")
    if plan.get("errored"):
        raise PlanError("the plan errored; fix the plan before asking for a policy verdict")
    settings = config or load_config()
    everything = resources(plan)
    refs = config_references(plan)
    found: List[Violation] = []
    for rule in RULES:
        found.extend(rule(everything, settings, refs))
    return sorted(found, key=lambda v: (v.rule, v.address))


def report(violations: List[Violation]) -> Dict[str, Any]:
    return {"passed": not violations, "violations": [asdict(v) for v in violations]}


def rule_ids() -> Tuple[str, ...]:
    return ("required-tags", "no-public-s3", "no-open-ingress", "encryption", "cidr-allocation")
