"""posture: scan the JSON that AWS describe and get commands print for insecure defaults (public buckets, admin ports open to the internet, unencrypted volumes, weak identities, missing audit logs) and report each finding with severity, evidence, fix and CIS AWS Foundations Benchmark section."""
from __future__ import annotations

import argparse
import base64
import csv
import io
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

BUNDLE_VERSION = 1
SEVERITIES = ("critical", "high", "medium", "low")
RANK = {name: index for index, name in enumerate(SEVERITIES)}
BENCHMARKS = ("5.0.0", "3.0.0")
ADMIN_PORTS = (22, 3389)
WORLD = {4: "0.0.0.0/0", 6: "::/0"}
BPA_FLAGS = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
ALL_BPA_ON = ",".join(f"{flag}=true" for flag in BPA_FLAGS)
NO_BPA = "NoSuchPublicAccessBlockConfiguration"
NO_POLICY = "NoSuchBucketPolicy"
NO_ENTITY = "NoSuchEntity"
ROOT_ROW = "<root_account>"
PUBLIC_GROUPS = {
    "http://acs.amazonaws.com/groups/global/AllUsers": "AllUsers (anyone on the internet)",
    "http://acs.amazonaws.com/groups/global/AuthenticatedUsers": "AuthenticatedUsers (anyone with any AWS account)",
}
FIXED_VALUE_KEYS = {
    "aws:sourcearn", "aws:sourcevpc", "aws:sourcevpce", "aws:sourceowner", "aws:sourceaccount",
    "aws:principalorgid", "aws:principalaccount", "aws:principalarn", "s3:dataaccesspointarn", "s3:dataaccesspointaccount",
}
EQUALITY_OPERATORS = {"stringequals", "stringequalsignorecase", "stringlike", "arnequals", "arnlike"}
WRITE_ACTIONS = ("s3:PutObject", "s3:DeleteObject", "s3:PutObjectAcl", "s3:PutBucketPolicy", "s3:PutBucketAcl", "s3:DeleteBucket")
TLS_PROBES = (("s3:GetObject", "/posture-probe"), ("s3:PutObject", "/posture-probe"), ("s3:ListBucket", ""))
COLLECT_CALLS = (
    ("sts", "get-caller-identity"),
    ("s3api", "list-buckets"),
    ("ec2", "describe-vpcs"),
    ("ec2", "describe-security-groups"),
    ("ec2", "describe-network-acls"),
    ("ec2", "describe-flow-logs"),
    ("ec2", "describe-volumes"),
    ("ec2", "describe-instances"),
    ("ec2", "get-ebs-encryption-by-default"),
    ("iam", "list-users"),
    ("iam", "get-account-summary"),
    ("iam", "get-account-password-policy"),
    ("cloudtrail", "describe-trails"),
    ("configservice", "describe-configuration-recorders"),
    ("configservice", "describe-configuration-recorder-status"),
    ("kms", "list-keys"),
    ("kms", "list-aliases"),
)
ERROR_CODE = re.compile(r"An error occurred \(([^)]+)\)")
RELATIVE = re.compile(r"^\+(\d+)d$")


class PostureError(Exception):
    pass


@dataclass(frozen=True)
class Check:
    id: str
    severity: str
    title: str
    cis5: str = ""
    cis3: str = ""
    securityhub: str = ""

    def cis(self, benchmark: str) -> str:
        return self.cis5 if benchmark == "5.0.0" else self.cis3

    def to_json(self) -> dict:
        return {"id": self.id, "severity": self.severity, "title": self.title, "cis": {"5.0.0": self.cis5, "3.0.0": self.cis3}, "securityhub": self.securityhub}


CHECKS: Dict[str, Check] = {check.id: check for check in (
    Check("s3-public-acl", "critical", "Bucket ACL grants access to everyone", securityhub="S3.2, S3.3"),
    Check("s3-public-policy", "critical", "Bucket policy is public", securityhub="S3.2, S3.3"),
    Check("s3-bucket-bpa-off", "high", "Bucket is not covered by all four Block Public Access settings", "2.1.4", "2.1.4", "S3.8"),
    Check("s3-account-bpa-off", "medium", "Account-level Block Public Access is not fully on", "2.1.4", "2.1.4", "S3.1"),
    Check("s3-tls-not-enforced", "medium", "Bucket policy does not deny requests made without TLS", "2.1.1", "2.1.1", "S3.5"),
    Check("sg-admin-ipv4", "high", "Security group allows 0.0.0.0/0 to an admin port", "5.3", "5.2", "EC2.53"),
    Check("sg-admin-ipv6", "high", "Security group allows ::/0 to an admin port", "5.4", "5.3", "EC2.54"),
    Check("default-sg-open", "medium", "Default security group of a VPC allows traffic", "5.5", "5.4", "EC2.2"),
    Check("nacl-admin-open", "medium", "Network ACL lets the whole internet reach an admin port", "5.2", "5.1", "EC2.21"),
    Check("ebs-unencrypted", "medium", "EBS volume is not encrypted", securityhub="EC2.3"),
    Check("ebs-default-encryption-off", "medium", "EBS encryption by default is off in this Region", "5.1.1", "2.2.1", "EC2.7"),
    Check("ec2-imdsv1", "high", "Instance still accepts IMDSv1", "5.7", "5.6", "EC2.8"),
    Check("root-access-key", "critical", "Root user has an access key", "1.3", "1.4", "IAM.4"),
    Check("root-mfa-off", "critical", "Root user has no MFA device", "1.4", "1.5", "IAM.9"),
    Check("root-used", "high", "Root user was used recently", "1.6", "1.7"),
    Check("user-console-no-mfa", "high", "IAM user with a console password has no MFA", "1.9", "1.10", "IAM.5"),
    Check("user-unused-credential", "medium", "IAM user credential unused for 45 days or more", "1.11", "1.12", "IAM.22"),
    Check("user-two-active-keys", "low", "IAM user has two active access keys", "1.12", "1.13"),
    Check("user-key-old", "medium", "Access key older than 90 days", "1.13", "1.14", "IAM.3"),
    Check("password-policy-length", "medium", "Password policy allows passwords shorter than 14 characters", "1.7", "1.8", "IAM.15"),
    Check("password-policy-reuse", "low", "Password policy does not prevent reuse of the last 24 passwords", "1.8", "1.9", "IAM.16"),
    Check("cloudtrail-no-multi-region", "high", "No multi-Region trail is logging all management events", "3.1", "3.1", "CloudTrail.1"),
    Check("cloudtrail-no-validation", "medium", "Trail log file validation is off", "3.2", "3.2", "CloudTrail.4"),
    Check("cloudtrail-no-kms", "medium", "Trail logs are not encrypted with a KMS key", "3.5", "3.5", "CloudTrail.2"),
    Check("config-not-recording", "medium", "AWS Config is not recording every resource type", "3.3", "3.3", "Config.1"),
    Check("kms-rotation-off", "medium", "Customer managed KMS key rotation is off", "3.6", "3.6", "KMS.4"),
    Check("vpc-flow-logs-off", "medium", "VPC has no active flow log", "3.7", "3.7", "EC2.6"),
)}


@dataclass
class Finding:
    check: Check
    severity: str
    resource: str
    name: str
    evidence: List[str]
    fix: str
    commands: List[List[str]] = field(default_factory=list)
    details: Dict[str, object] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "check": self.check.id,
            "severity": self.severity,
            "title": self.check.title,
            "resource": self.resource,
            "name": self.name,
            "cis": {"5.0.0": self.check.cis5, "3.0.0": self.check.cis3},
            "securityhub": self.check.securityhub,
            "evidence": list(self.evidence),
            "fix": self.fix,
            "commands": [list(command) for command in self.commands],
            "details": dict(self.details),
        }


@dataclass
class Settings:
    now: datetime
    benchmark: str = "5.0.0"
    key_max_age_days: int = 90
    unused_days: int = 45
    root_used_days: int = 90


@dataclass
class CallResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], CallResult]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: object) -> Optional[datetime]:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    if not isinstance(value, str) or not value[:4].isdigit():
        return None
    text = value.strip().replace("Z", "+00:00")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        text += "T00:00:00+00:00"
    text = re.sub(r"\.(\d{6})\d+", r".\1", text)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def days_since(then: Optional[datetime], now: datetime) -> Optional[int]:
    return None if then is None else (now - then).days


def as_list(value: object) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def name_tag(tags: object, default: str) -> str:
    for tag in as_list(tags):
        if isinstance(tag, dict) and tag.get("Key") == "Name" and tag.get("Value"):
            return str(tag["Value"])
    return default


def compact_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def record_call(runner: Runner, argv: Sequence[str]) -> dict:
    result = runner(list(argv))
    if result.returncode == 0:
        text = result.stdout.strip()
        try:
            return {"ok": True, "output": json.loads(text) if text else {}}
        except ValueError:
            return {"ok": False, "error": "UnparsableOutput", "message": text[:300]}
    match = ERROR_CODE.search(result.stderr)
    return {"ok": False, "error": match.group(1) if match else "CommandFailed", "message": result.stderr.strip()[-300:]}


def aws_cli(endpoint: str = "", region: str = "", binary: str = "aws") -> Runner:
    def run(argv: Sequence[str]) -> CallResult:
        command = [binary, *argv, "--output", "json"]
        if region:
            command += ["--region", region]
        if endpoint:
            command += ["--endpoint-url", endpoint]
        try:
            proc = subprocess.run(command, capture_output=True, text=True, env=dict(os.environ, AWS_PAGER=""), timeout=120)
        except FileNotFoundError as exc:
            raise PostureError(f"{binary} not found: install the AWS CLI or pass --aws PATH") from exc
        return CallResult(proc.returncode, proc.stdout, proc.stderr)
    return run


def collect(runner: Runner, region: str, clock: Callable[[], datetime] = utcnow, sleep: Callable[[float], None] = time.sleep, report_attempts: int = 10) -> dict:
    calls: Dict[str, dict] = {}

    def call(*argv: str) -> dict:
        calls[shlex.join(argv)] = record_call(runner, argv)
        return calls[shlex.join(argv)]

    def output(entry: dict) -> dict:
        return entry.get("output") or {} if entry.get("ok") else {}

    for argv in COLLECT_CALLS:
        call(*argv)
    account = str(output(calls["sts get-caller-identity"]).get("Account", ""))
    if account:
        call("s3control", "get-public-access-block", "--account-id", account)
    for bucket in output(calls["s3api list-buckets"]).get("Buckets") or []:
        for command in ("get-public-access-block", "get-bucket-acl", "get-bucket-policy"):
            call("s3api", command, "--bucket", bucket["Name"])
    for trail in output(calls["cloudtrail describe-trails"]).get("trailList") or []:
        ref = trail["Name"] if trail.get("HomeRegion", region) == region else trail["TrailARN"]
        call("cloudtrail", "get-trail-status", "--name", ref)
        call("cloudtrail", "get-event-selectors", "--trail-name", ref)
    for user in output(calls["iam list-users"]).get("Users") or []:
        keys = call("iam", "list-access-keys", "--user-name", user["UserName"])
        for key in output(keys).get("AccessKeyMetadata") or []:
            call("iam", "get-access-key-last-used", "--access-key-id", key["AccessKeyId"])
    for key in output(calls["kms list-keys"]).get("Keys") or []:
        meta = output(call("kms", "describe-key", "--key-id", key["KeyId"])).get("KeyMetadata") or {}
        if rotatable(meta):
            call("kms", "get-key-rotation-status", "--key-id", key["KeyId"])
    for _ in range(report_attempts):
        state = output(call("iam", "generate-credential-report")).get("State")
        if state == "COMPLETE":
            break
        sleep(1)
    call("iam", "get-credential-report")
    return {"posture_bundle": BUNDLE_VERSION, "account": account, "region": region, "collected_at": iso(clock()), "calls": calls}


def rotatable(meta: dict) -> bool:
    return (
        meta.get("KeyManager") == "CUSTOMER"
        and meta.get("KeySpec", meta.get("CustomerMasterKeySpec")) == "SYMMETRIC_DEFAULT"
        and meta.get("KeyState") == "Enabled"
        and meta.get("Origin", "AWS_KMS") == "AWS_KMS"
    )


def glob_match(pattern: str, value: str, ignore_case: bool = False) -> bool:
    regex = "".join(".*" if char == "*" else "." if char == "?" else re.escape(char) for char in pattern)
    return re.fullmatch(regex, value, re.IGNORECASE if ignore_case else 0) is not None


def statements(policy: dict) -> List[dict]:
    return [statement for statement in as_list(policy.get("Statement")) if isinstance(statement, dict)]


def principal_is_anyone(statement: dict) -> bool:
    if "NotPrincipal" in statement:
        return statement.get("Effect") == "Allow"
    principal = statement.get("Principal")
    if principal == "*":
        return True
    if isinstance(principal, dict):
        return any(isinstance(value, str) and "*" in value for values in principal.values() for value in as_list(values))
    return False


def fixed_value(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not any(token in value for token in ("*", "?", "${"))


def narrow_cidr(value: object) -> bool:
    try:
        network = ipaddress.ip_network(str(value), strict=False)
    except ValueError:
        return False
    return network.prefixlen >= (8 if network.version == 4 else 32)


def condition_restricts(condition: object) -> bool:
    for operator, block in (condition or {}).items() if isinstance(condition, dict) else []:
        name = operator.lower()
        if name.startswith("forallvalues:") or name.endswith("ifexists") or "not" in name.split(":")[-1]:
            continue
        base = name.split(":")[-1]
        for key, values in (block or {}).items():
            wanted = as_list(values)
            if not wanted:
                continue
            if key.lower() == "aws:sourceip" and base == "ipaddress" and all(narrow_cidr(v) for v in wanted):
                return True
            if key.lower() in FIXED_VALUE_KEYS and base in EQUALITY_OPERATORS and all(fixed_value(v) for v in wanted):
                return True
    return False


def statement_is_public(statement: dict) -> bool:
    return statement.get("Effect") == "Allow" and principal_is_anyone(statement) and not condition_restricts(statement.get("Condition"))


def condition_holds(operator: str, key: str, wanted: list, context: Dict[str, List[str]]) -> Optional[bool]:
    name = operator.lower()
    if_exists = name.endswith("ifexists")
    base = name[:-len("ifexists")] if if_exists else name
    present = context.get(key.lower())
    negated = base in {"stringnotequals", "stringnotequalsignorecase", "stringnotlike", "arnnotequals", "arnnotlike", "notipaddress"}
    if present is None:
        return True if if_exists or negated else False
    values = [str(v) for v in wanted]
    if base == "bool":
        return any(actual.lower() == v.lower() for actual in present for v in values)
    if base in {"stringequals", "stringnotequals", "arnequals", "arnnotequals"}:
        hit = any(actual == v for actual in present for v in values)
    elif base in {"stringequalsignorecase", "stringnotequalsignorecase"}:
        hit = any(actual.lower() == v.lower() for actual in present for v in values)
    elif base in {"stringlike", "stringnotlike", "arnlike", "arnnotlike"}:
        hit = any(glob_match(v, actual) for actual in present for v in values)
    elif base in {"ipaddress", "notipaddress"}:
        hit = any(ip_in(actual, v) for actual in present for v in values)
    else:
        return None
    return not hit if negated else hit


def ip_in(address: str, cidr: str) -> bool:
    try:
        return ipaddress.ip_address(address) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False


def statement_applies(statement: dict, action: str, resource: str, context: Dict[str, List[str]]) -> bool:
    if "Action" in statement and not any(glob_match(p, action, True) for p in as_list(statement["Action"])):
        return False
    if "NotAction" in statement and any(glob_match(p, action, True) for p in as_list(statement["NotAction"])):
        return False
    if "Resource" in statement and not any(glob_match(p, resource) for p in as_list(statement["Resource"])):
        return False
    if "NotResource" in statement and any(glob_match(p, resource) for p in as_list(statement["NotResource"])):
        return False
    for operator, block in (statement.get("Condition") or {}).items():
        for key, values in (block or {}).items():
            if condition_holds(operator, key, as_list(values), context) is not True:
                return False
    return True


def tls_gap(policy: dict, bucket: str) -> Optional[str]:
    context = {"aws:securetransport": ["false"]}
    for action, suffix in TLS_PROBES:
        resource = f"arn:aws:s3:::{bucket}{suffix}"
        denied = any(
            s.get("Effect") == "Deny" and s.get("Principal") in ("*", {"AWS": "*"}) and statement_applies(s, action, resource, context)
            for s in statements(policy)
        )
        if not denied:
            return f"{action} on {resource} over plain HTTP (aws:SecureTransport=false) is not explicitly denied"
    return None


def grants_write(statement: dict) -> bool:
    patterns = as_list(statement.get("Action"))
    if "NotAction" in statement:
        return True
    return any(glob_match(p, action, True) for p in patterns for action in WRITE_ACTIONS)


def describe_statement(statement: dict, index: int) -> str:
    label = statement.get("Sid") or f"Statement[{index}]"
    who = compact_json(statement.get("Principal", {"NotPrincipal": statement.get("NotPrincipal")}))
    actions = ", ".join(as_list(statement.get("Action") or statement.get("NotAction")))
    resources = ", ".join(as_list(statement.get("Resource") or statement.get("NotResource")))
    condition = compact_json(statement["Condition"]) if statement.get("Condition") else "no condition"
    return f"{label}: Allow {who} {actions} on {resources} ({condition}; nothing limits it to a fixed account, VPC, endpoint or narrow IP range)"


def covers_port(protocol: object, low: object, high: object, port: int) -> bool:
    proto = str(protocol).lower()
    if proto in ("-1", "all"):
        return True
    if proto not in ("tcp", "6", "udp", "17") or low is None or high is None:
        return False
    return int(low) <= port <= int(high)


def describe_rule(protocol: object, low: object, high: object) -> str:
    proto = {"-1": "all traffic", "6": "tcp", "17": "udp"}.get(str(protocol), str(protocol))
    if proto == "all traffic":
        return proto
    return f"{proto} {low}" if low == high else f"{proto} {low}-{high}"


def clean_permission(permission: dict) -> dict:
    keep = ("IpProtocol", "FromPort", "ToPort", "IpRanges", "Ipv6Ranges", "UserIdGroupPairs", "PrefixListIds")
    out = {key: permission[key] for key in keep if key in permission and permission[key] not in ([], None)}
    if out.get("IpProtocol") == "-1":
        out.pop("FromPort", None)
        out.pop("ToPort", None)
    return out


class Scan:
    def __init__(self, bundle: dict, settings: Settings):
        if not isinstance(bundle, dict) or bundle.get("posture_bundle") != BUNDLE_VERSION or not isinstance(bundle.get("calls"), dict):
            raise PostureError("not a posture bundle: expected {\"posture_bundle\": 1, \"calls\": {...}}; make one with: posture collect -o bundle.json")
        self.calls: Dict[str, dict] = bundle["calls"]
        self.account = str(bundle.get("account") or "")
        self.region = str(bundle.get("region") or "")
        self.settings = settings
        self.findings: List[Finding] = []
        self.gaps: List[Dict[str, str]] = []

    def read(self, *argv: str, absent: Iterable[str] = ()) -> Optional[dict]:
        key = shlex.join(argv)
        entry = self.calls.get(key)
        if entry is None:
            self.gaps.append({"call": key, "reason": "not collected"})
            return None
        if entry.get("ok"):
            output = entry.get("output")
            return output if isinstance(output, dict) else {}
        if entry.get("error") in set(absent):
            return {}
        self.gaps.append({"call": key, "reason": f"{entry.get('error')}: {entry.get('message', '')}".strip()})
        return None

    def add(self, check_id: str, resource: str, name: str, evidence: List[str], fix: str, commands: Sequence[Sequence[str]] = (), severity: str = "", details: Optional[dict] = None) -> None:
        check = CHECKS[check_id]
        self.findings.append(Finding(check, severity or check.severity, resource, name, evidence, fix, [list(c) for c in commands], details or {}))

    def region_args(self) -> List[str]:
        return ["--region", self.region] if self.region else []


def bpa_flags(doc: Optional[dict]) -> Optional[Dict[str, bool]]:
    if doc is None:
        return None
    config = doc.get("PublicAccessBlockConfiguration") or {}
    return {flag: bool(config.get(flag)) for flag in BPA_FLAGS}


def effective_bpa(bucket: Optional[Dict[str, bool]], account: Optional[Dict[str, bool]]) -> Dict[str, bool]:
    return {flag: bool((bucket or {}).get(flag)) or bool((account or {}).get(flag)) for flag in BPA_FLAGS}


def public_acl_grants(acl: dict) -> List[str]:
    grants = []
    for grant in acl.get("Grants") or []:
        uri = (grant.get("Grantee") or {}).get("URI", "")
        if uri in PUBLIC_GROUPS:
            grants.append(f"{PUBLIC_GROUPS[uri]} has {grant.get('Permission')}")
    return grants


def check_s3(scan: Scan) -> None:
    listing = scan.read("s3api", "list-buckets")
    if listing is None:
        return
    account = None
    if scan.account:
        account = bpa_flags(scan.read("s3control", "get-public-access-block", "--account-id", scan.account, absent=(NO_BPA,)))
    if account is not None and not all(account.values()):
        off = [flag for flag, on in account.items() if not on]
        scan.add("s3-account-bpa-off", f"arn:aws:s3:::account/{scan.account}", "account",
                 [f"s3control get-public-access-block --account-id {scan.account}: {', '.join(off)} not true" if any(account.values()) else "no account-level Block Public Access configuration"],
                 "Turn on all four settings for the account; every current and future bucket inherits them.",
                 [["s3control", "put-public-access-block", "--account-id", scan.account, "--public-access-block-configuration", ALL_BPA_ON]],
                 details={"off": off})
    for bucket in listing.get("Buckets") or []:
        check_bucket(scan, bucket["Name"], account)


def check_bucket(scan: Scan, name: str, account: Optional[Dict[str, bool]]) -> None:
    arn = f"arn:aws:s3:::{name}"
    own = bpa_flags(scan.read("s3api", "get-public-access-block", "--bucket", name, absent=(NO_BPA,)))
    effective = effective_bpa(own, account)
    bpa_command = ["s3api", "put-public-access-block", "--bucket", name, "--public-access-block-configuration", ALL_BPA_ON]
    if not all(effective.values()):
        off = [flag for flag, on in effective.items() if not on]
        level = "bucket settings unknown" if own is None else ("no bucket-level configuration" if not any(own.values()) else "bucket-level " + ", ".join(f for f, v in own.items() if not v) + " false")
        scope = "account-level settings unknown" if account is None else ("account-level settings off" if not any(account.values()) else "account-level " + ", ".join(f for f, v in account.items() if not v) + " false")
        scan.add("s3-bucket-bpa-off", arn, name, [f"effective (bucket OR account) settings: {', '.join(off)} not on ({level}; {scope})"],
                 "Turn on all four Block Public Access settings for the bucket unless it is a reviewed public site.", [bpa_command], details={"off": off})
    acl = scan.read("s3api", "get-bucket-acl", "--bucket", name)
    grants = public_acl_grants(acl) if acl is not None else []
    if grants:
        ignored = effective["IgnorePublicAcls"]
        scan.add("s3-public-acl", arn, name,
                 [f"s3api get-bucket-acl --bucket {name}: {grant}" for grant in grants] + (["neutralised: IgnorePublicAcls is on, so S3 ignores these grants until someone turns it off"] if ignored else []),
                 "Replace the ACL with a private one, then disable ACLs with Object Ownership BucketOwnerEnforced (aws s3api put-bucket-ownership-controls --bucket "
                 f"{name} --ownership-controls 'Rules=[{{ObjectOwnership=BucketOwnerEnforced}}]').",
                 [["s3api", "put-bucket-acl", "--bucket", name, "--acl", "private"]], severity="low" if ignored else "", details={"grants": grants, "neutralised": ignored})
    policy_doc = scan.read("s3api", "get-bucket-policy", "--bucket", name, absent=(NO_POLICY,))
    if policy_doc is None:
        return
    policy: dict = {}
    if policy_doc.get("Policy"):
        try:
            policy = json.loads(policy_doc["Policy"])
        except ValueError:
            scan.gaps.append({"call": f"s3api get-bucket-policy --bucket {name}", "reason": "Policy is not valid JSON"})
            return
    public = [(index, s) for index, s in enumerate(statements(policy)) if statement_is_public(s)]
    if public:
        restricted = effective["RestrictPublicBuckets"]
        scan.add("s3-public-policy", arn, name,
                 [describe_statement(s, index) for index, s in public] + (["neutralised: RestrictPublicBuckets is on, so only AWS services and the bucket owner's account can use these statements"] if restricted else []),
                 "Remove the public statements or add a condition with a fixed aws:SourceVpce, aws:SourceVpc, aws:PrincipalOrgID or narrow aws:SourceIp; BlockPublicPolicy and RestrictPublicBuckets stop it coming back.",
                 [bpa_command], severity="low" if restricted else "",
                 details={"statements": [s.get("Sid") or f"Statement[{i}]" for i, s in public], "write": any(grants_write(s) for _, s in public), "neutralised": restricted})
    gap = tls_gap(policy, name)
    if gap:
        deny = {"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": [arn, f"{arn}/*"], "Condition": {"Bool": {"aws:SecureTransport": "false"}}}
        scan.add("s3-tls-not-enforced", arn, name, ["no bucket policy" if not policy else gap],
                 f"Add this statement to the bucket policy (merge it, put-bucket-policy replaces the whole document): {compact_json(deny)}")


def check_security_groups(scan: Scan, groups: List[dict], vpc_names: Dict[str, str]) -> None:
    for group in groups:
        gid, name = group.get("GroupId", ""), group.get("GroupName", "")
        for family, range_key, cidr_key in ((4, "IpRanges", "CidrIp"), (6, "Ipv6Ranges", "CidrIpv6")):
            ports, evidence, commands = set(), [], []
            for index, permission in enumerate(group.get("IpPermissions") or []):
                open_ports = [p for p in ADMIN_PORTS if covers_port(permission.get("IpProtocol"), permission.get("FromPort"), permission.get("ToPort"), p)]
                if not open_ports or not any(r.get(cidr_key) == WORLD[family] for r in permission.get(range_key) or []):
                    continue
                ports.update(open_ports)
                evidence.append(f"IpPermissions[{index}]: {describe_rule(permission.get('IpProtocol'), permission.get('FromPort'), permission.get('ToPort'))} from {WORLD[family]} reaches port {', '.join(map(str, open_ports))}")
                revoke = clean_permission({**permission, "IpRanges": [], "Ipv6Ranges": [], "UserIdGroupPairs": [], "PrefixListIds": [], range_key: [{cidr_key: WORLD[family]}]})
                commands.append(["ec2", "revoke-security-group-ingress", "--group-id", gid, "--ip-permissions", compact_json([revoke])] + scan.region_args())
            if ports:
                scan.add(f"sg-admin-ipv{family}", gid, name, evidence,
                         "Revoke the rule and reach the hosts through SSM Session Manager, a bastion with a narrow CIDR, or a VPN.",
                         commands, details={"ports": sorted(ports), "vpc": vpc_names.get(group.get("VpcId", ""), group.get("VpcId", ""))})
        if name == "default" and (group.get("IpPermissions") or group.get("IpPermissionsEgress")):
            vpc = group.get("VpcId", "")
            commands = []
            if group.get("IpPermissions"):
                commands.append(["ec2", "revoke-security-group-ingress", "--group-id", gid, "--ip-permissions", compact_json([clean_permission(p) for p in group["IpPermissions"]])] + scan.region_args())
            if group.get("IpPermissionsEgress"):
                commands.append(["ec2", "revoke-security-group-egress", "--group-id", gid, "--ip-permissions", compact_json([clean_permission(p) for p in group["IpPermissionsEgress"]])] + scan.region_args())
            scan.add("default-sg-open", gid, vpc_names.get(vpc, vpc),
                     [f"{gid} in {vpc}: {len(group.get('IpPermissions') or [])} inbound and {len(group.get('IpPermissionsEgress') or [])} outbound rule(s)"],
                     "Remove every rule from the default group and give workloads their own groups; anything launched without a group lands in this one.",
                     commands, details={"vpc": vpc})


def nacl_exposure(entries: List[dict], port: int, family: int) -> Optional[dict]:
    key = "CidrBlock" if family == 4 else "Ipv6CidrBlock"
    for entry in sorted((e for e in entries if not e.get("Egress")), key=lambda e: int(e.get("RuleNumber", 0))):
        if entry.get(key) != WORLD[family]:
            continue
        span = entry.get("PortRange") or {}
        if covers_port(entry.get("Protocol"), span.get("From"), span.get("To"), port):
            return entry if entry.get("RuleAction") == "allow" else None
    return None


def free_rule_numbers(entries: List[dict], below: int, count: int) -> List[int]:
    used = {int(e.get("RuleNumber", 0)) for e in entries if not e.get("Egress")}
    numbers = [n for n in range(below - 1, 0, -1) if n not in used][:count]
    return numbers if len(numbers) == count else []


def check_nacls(scan: Scan, acls: List[dict]) -> None:
    for acl in acls:
        if acl.get("IsDefault"):
            continue
        entries = acl.get("Entries") or []
        exposures = []
        for family in (4, 6):
            for port in ADMIN_PORTS:
                entry = nacl_exposure(entries, port, family)
                if entry:
                    exposures.append((family, port, entry))
        if not exposures:
            continue
        acl_id = acl.get("NetworkAclId", "")
        protocols = [(family, port, proto) for family, port, entry in exposures for proto in (("6", "17") if str(entry.get("Protocol")) == "-1" else (str({"tcp": "6", "udp": "17"}.get(str(entry.get("Protocol")), entry.get("Protocol"))),))]
        numbers = free_rule_numbers(entries, min(int(e.get("RuleNumber", 0)) for _, _, e in exposures), len(protocols))
        commands = [
            ["ec2", "create-network-acl-entry", "--network-acl-id", acl_id, "--ingress", "--rule-number", str(number), "--protocol", proto,
             "--port-range", f"From={port},To={port}", "--cidr-block" if family == 4 else "--ipv6-cidr-block", WORLD[family], "--rule-action", "deny"] + scan.region_args()
            for number, (family, port, proto) in zip(numbers, protocols)
        ]
        scan.add("nacl-admin-open", acl_id, name_tag(acl.get("Tags"), acl_id),
                 [f"port {port} from {WORLD[family]}: the first matching inbound rule is {entry.get('RuleNumber')} allow {describe_rule(entry.get('Protocol'), (entry.get('PortRange') or {}).get('From'), (entry.get('PortRange') or {}).get('To'))}" for family, port, entry in exposures],
                 "Insert deny rules for the admin ports with lower rule numbers than the allow (rules are evaluated in order), or narrow the allow's CIDR.",
                 commands, details={"ports": sorted({port for _, port, _ in exposures}), "rules": sorted({int(e.get("RuleNumber", 0)) for _, _, e in exposures})})


def check_network(scan: Scan) -> None:
    vpcs_doc = scan.read("ec2", "describe-vpcs")
    vpcs = (vpcs_doc or {}).get("Vpcs") or []
    vpc_names = {v.get("VpcId", ""): name_tag(v.get("Tags"), v.get("VpcId", "")) for v in vpcs}
    groups = scan.read("ec2", "describe-security-groups")
    if groups is not None:
        check_security_groups(scan, groups.get("SecurityGroups") or [], vpc_names)
    acls = scan.read("ec2", "describe-network-acls")
    if acls is not None:
        check_nacls(scan, acls.get("NetworkAcls") or [])
    flows = scan.read("ec2", "describe-flow-logs")
    if vpcs_doc is not None and flows is not None:
        logged = {f.get("ResourceId") for f in flows.get("FlowLogs") or [] if f.get("FlowLogStatus", "ACTIVE") == "ACTIVE" and f.get("TrafficType") in ("ALL", "REJECT")}
        for vpc in vpcs:
            vid = vpc.get("VpcId", "")
            if vid not in logged:
                scan.add("vpc-flow-logs-off", vid, vpc_names[vid], [f"no ACTIVE flow log with TrafficType ALL or REJECT has ResourceId {vid}"],
                         f"aws ec2 create-flow-logs --resource-type VPC --resource-ids {vid} --traffic-type REJECT --log-destination-type s3 --log-destination arn:aws:s3:::BUCKET (or cloud-watch-logs with --deliver-logs-permission-arn)")


def check_compute(scan: Scan) -> None:
    default = scan.read("ec2", "get-ebs-encryption-by-default")
    if default is not None and not default.get("EbsEncryptionByDefault"):
        scan.add("ebs-default-encryption-off", f"ebs-encryption-by-default/{scan.region}", "account", [f"ec2 get-ebs-encryption-by-default: EbsEncryptionByDefault=false in {scan.region}"],
                 "Turn it on in every Region you use; new volumes and snapshot copies are then encrypted with the default EBS key.",
                 [["ec2", "enable-ebs-encryption-by-default"] + scan.region_args()])
    volumes = scan.read("ec2", "describe-volumes")
    for volume in (volumes or {}).get("Volumes") or []:
        if volume.get("Encrypted"):
            continue
        vid = volume.get("VolumeId", "")
        attached = [f"{a.get('InstanceId')} as {a.get('Device')}" for a in volume.get("Attachments") or []]
        scan.add("ebs-unencrypted", vid, name_tag(volume.get("Tags"), vid),
                 [f"{vid}: Encrypted=false, State={volume.get('State')}, {volume.get('Size')} GiB" + (f", attached to {', '.join(attached)}" if attached else ", not attached")],
                 f"EBS cannot encrypt in place: aws ec2 create-snapshot --volume-id {vid}, aws ec2 copy-snapshot --encrypted, create a volume from the copy and swap it in.",
                 severity="medium" if volume.get("State") == "in-use" else "low", details={"state": volume.get("State")})
    reservations = scan.read("ec2", "describe-instances")
    for reservation in (reservations or {}).get("Reservations") or []:
        for instance in reservation.get("Instances") or []:
            options = instance.get("MetadataOptions") or {}
            if (instance.get("State") or {}).get("Name") == "terminated" or options.get("HttpEndpoint") == "disabled" or options.get("HttpTokens") == "required":
                continue
            iid = instance.get("InstanceId", "")
            scan.add("ec2-imdsv1", iid, name_tag(instance.get("Tags"), iid), [f"{iid}: MetadataOptions.HttpTokens={options.get('HttpTokens', 'optional')}, HttpEndpoint={options.get('HttpEndpoint', 'enabled')}"],
                     "Require session tokens (IMDSv2) so an SSRF bug cannot read the instance role's credentials with a plain GET.",
                     [["ec2", "modify-instance-metadata-options", "--instance-id", iid, "--http-tokens", "required", "--http-endpoint", "enabled"] + scan.region_args()])


def credential_rows(doc: Optional[dict]) -> Optional[List[dict]]:
    if doc is None or not doc.get("Content"):
        return None
    content = doc["Content"]
    raw = base64.b64decode(content).decode("utf-8") if isinstance(content, str) else bytes(content).decode("utf-8")
    return list(csv.DictReader(io.StringIO(raw)))


def check_root(scan: Scan, rows: Optional[List[dict]]) -> None:
    root = next((row for row in rows or [] if row.get("user") == ROOT_ROW), None)
    arn = f"arn:aws:iam::{scan.account}:root"
    now = scan.settings.now
    if root is None:
        summary = scan.read("iam", "get-account-summary")
        if summary is None:
            return
        counts = summary.get("SummaryMap") or {}
        if counts.get("AccountAccessKeysPresent"):
            scan.add("root-access-key", arn, "root", ["iam get-account-summary: AccountAccessKeysPresent=1"], "Sign in as root, delete the access key under Security credentials, and use IAM roles instead.")
        if not counts.get("AccountMFAEnabled"):
            scan.add("root-mfa-off", arn, "root", ["iam get-account-summary: AccountMFAEnabled=0"], "Sign in as root and register MFA (a hardware key or passkey); keep the device in a safe with a second one.")
        scan.gaps.append({"call": "iam get-credential-report", "reason": f"no {ROOT_ROW} row: root-used not evaluated"})
        return
    active = [slot for slot in ("1", "2") if root.get(f"access_key_{slot}_active") == "true"]
    if active:
        scan.add("root-access-key", arn, "root", [f"credential report {ROOT_ROW}: access_key_{slot}_active=true" for slot in active],
                 "Sign in as root, delete the access key under Security credentials, and use IAM roles instead.")
    if root.get("mfa_active") != "true":
        scan.add("root-mfa-off", arn, "root", [f"credential report {ROOT_ROW}: mfa_active={root.get('mfa_active')}"],
                 "Sign in as root and register MFA (a hardware key or passkey); keep the device in a safe with a second one.")
    uses = [(column, parse_time(root.get(column))) for column in ("password_last_used", "access_key_1_last_used_date", "access_key_2_last_used_date")]
    recent = [(column, when) for column, when in uses if when is not None and days_since(when, now) is not None and days_since(when, now) < scan.settings.root_used_days]
    if recent:
        scan.add("root-used", arn, "root", [f"credential report {ROOT_ROW}: {column}={iso(when)} ({days_since(when, now)} days ago)" for column, when in recent],
                 "Find out who used root and why: aws cloudtrail lookup-events --lookup-attributes AttributeKey=Username,AttributeValue=root. Give that task an IAM role and lock root away.",
                 details={"days": scan.settings.root_used_days})


def check_users(scan: Scan, rows: Optional[List[dict]]) -> None:
    users = scan.read("iam", "list-users")
    if users is None:
        return
    by_name = {row.get("user"): row for row in rows or []}
    now, settings = scan.settings.now, scan.settings
    for user in users.get("Users") or []:
        name, arn = user.get("UserName", ""), user.get("Arn", "")
        row = by_name.get(name)
        if row and row.get("password_enabled") == "true" and row.get("mfa_active") != "true":
            scan.add("user-console-no-mfa", arn, name, [f"credential report {name}: password_enabled=true, mfa_active={row.get('mfa_active')}"],
                     f"Register MFA for {name}, or remove the console password if the user only needs API access: aws iam delete-login-profile --user-name {name}.")
        unused, unused_commands = [], []
        if row and row.get("password_enabled") == "true":
            last = parse_time(row.get("password_last_used")) or parse_time(row.get("user_creation_time")) or parse_time(user.get("CreateDate"))
            age = days_since(last, now)
            if age is not None and age >= settings.unused_days:
                unused.append(f"console password: last used or created {age} days ago (password_last_used={row.get('password_last_used')})")
        listing = scan.read("iam", "list-access-keys", "--user-name", name)
        keys = [k for k in (listing or {}).get("AccessKeyMetadata") or [] if k.get("Status") == "Active"]
        old, old_commands = [], []
        for key in keys:
            kid, created = key.get("AccessKeyId", ""), parse_time(key.get("CreateDate"))
            deactivate = ["iam", "update-access-key", "--user-name", name, "--access-key-id", kid, "--status", "Inactive"]
            used_doc = scan.read("iam", "get-access-key-last-used", "--access-key-id", kid)
            if used_doc is not None:
                last_used = parse_time((used_doc.get("AccessKeyLastUsed") or {}).get("LastUsedDate"))
                idle = days_since(last_used or created, now)
                if idle is not None and idle >= settings.unused_days:
                    unused.append(f"access key {kid}: " + (f"last used {idle} days ago" if last_used else f"never used, created {idle} days ago"))
                    unused_commands.append(deactivate)
            age = days_since(created, now)
            if age is not None and age > settings.key_max_age_days:
                old.append(f"access key {kid}: created {iso(created)}, {age} days old")
                old_commands.append(deactivate)
        if unused:
            scan.add("user-unused-credential", arn, name, unused, "Deactivate credentials nobody uses; delete them after a quiet period.", unused_commands, details={"days": settings.unused_days})
        if len(keys) == 2:
            scan.add("user-two-active-keys", arn, name, [f"iam list-access-keys --user-name {name}: {keys[0].get('AccessKeyId')} and {keys[1].get('AccessKeyId')} are both Active"],
                     "Keep one active key; the second slot exists for rotation, not as a spare.")
        if old:
            scan.add("user-key-old", arn, name, old, f"Rotate: create a new key, deploy it, then deactivate and delete the old one. Better, replace {name}'s keys with a role.", old_commands, details={"days": settings.key_max_age_days})


def password_policy_command(current: dict) -> List[str]:
    command = ["iam", "update-account-password-policy",
               "--minimum-password-length", str(max(14, int(current.get("MinimumPasswordLength") or 0))),
               "--password-reuse-prevention", str(max(24, int(current.get("PasswordReusePrevention") or 0)))]
    for key, flag in (("RequireSymbols", "--require-symbols"), ("RequireNumbers", "--require-numbers"), ("RequireUppercaseCharacters", "--require-uppercase-characters"),
                      ("RequireLowercaseCharacters", "--require-lowercase-characters"), ("AllowUsersToChangePassword", "--allow-users-to-change-password"), ("HardExpiry", "--hard-expiry")):
        if current.get(key):
            command.append(flag)
    if current.get("ExpirePasswords") and current.get("MaxPasswordAge"):
        command += ["--max-password-age", str(current["MaxPasswordAge"])]
    return command


def check_password_policy(scan: Scan) -> None:
    doc = scan.read("iam", "get-account-password-policy", absent=(NO_ENTITY,))
    if doc is None:
        return
    current = doc.get("PasswordPolicy") or {}
    resource = f"arn:aws:iam::{scan.account}:account-password-policy"
    command = password_policy_command(current)
    note = "update-account-password-policy replaces the whole policy, so the command repeats the settings you already have."
    length_fix, reuse_fix = f"Require at least 14 characters. {note}", f"Remember the last 24 passwords. {note}"
    if not current:
        scan.add("password-policy-length", resource, "account", ["no account password policy: IAM's default allows 8-character passwords"], length_fix, [command])
        scan.add("password-policy-reuse", resource, "account", ["no account password policy: reuse is not prevented"], reuse_fix, [command])
        return
    if int(current.get("MinimumPasswordLength") or 0) < 14:
        scan.add("password-policy-length", resource, "account", [f"MinimumPasswordLength={current.get('MinimumPasswordLength')}"], length_fix, [command])
    if int(current.get("PasswordReusePrevention") or 0) < 24:
        scan.add("password-policy-reuse", resource, "account", [f"PasswordReusePrevention={current.get('PasswordReusePrevention', 'not set')}"], reuse_fix, [command])


def logs_all_management(trail: dict, selectors: Optional[dict]) -> bool:
    if not trail.get("HasCustomEventSelectors"):
        return True
    for selector in (selectors or {}).get("EventSelectors") or []:
        if selector.get("IncludeManagementEvents") and selector.get("ReadWriteType", "All") == "All":
            return True
    for advanced in (selectors or {}).get("AdvancedEventSelectors") or []:
        fields = {f.get("Field"): f for f in advanced.get("FieldSelectors") or []}
        if (fields.get("eventCategory") or {}).get("Equals") == ["Management"] and "readOnly" not in fields:
            return True
    return False


def check_logging(scan: Scan) -> None:
    described = scan.read("cloudtrail", "describe-trails")
    if described is not None:
        trails = list({t.get("TrailARN"): t for t in described.get("trailList") or []}.values())
        coverage, good = [], False
        for trail in trails:
            ref = trail["Name"] if trail.get("HomeRegion", scan.region) == scan.region else trail["TrailARN"]
            status = scan.read("cloudtrail", "get-trail-status", "--name", ref)
            selectors = scan.read("cloudtrail", "get-event-selectors", "--trail-name", ref)
            logging = bool((status or {}).get("IsLogging"))
            management = logs_all_management(trail, selectors)
            good = good or (bool(trail.get("IsMultiRegionTrail")) and logging and management)
            coverage.append((trail, ref, logging, management))
            if not trail.get("LogFileValidationEnabled"):
                scan.add("cloudtrail-no-validation", trail.get("TrailARN", ""), trail.get("Name", ""), [f"{trail.get('Name')}: LogFileValidationEnabled=false"],
                         "Turn on digest files so you can prove later that no log file was changed or deleted.", [["cloudtrail", "update-trail", "--name", ref, "--enable-log-file-validation"] + scan.region_args()])
            if not trail.get("KmsKeyId"):
                scan.add("cloudtrail-no-kms", trail.get("TrailARN", ""), trail.get("Name", ""), [f"{trail.get('Name')}: no KmsKeyId (SSE-S3 only)"],
                         f"aws cloudtrail update-trail --name {ref} --kms-key-id alias/KEY, with a key policy that lets cloudtrail.amazonaws.com encrypt and your auditors decrypt.")
        if not good:
            evidence = [f"{t.get('Name')}: IsMultiRegionTrail={str(bool(t.get('IsMultiRegionTrail'))).lower()}, IsLogging={str(on).lower()}, all management events={str(m).lower()}" for t, _, on, m in coverage] or ["describe-trails: no trails"]
            commands = []
            multi = [(t, ref) for t, ref, on, m in coverage if t.get("IsMultiRegionTrail") and m]
            single = [(t, ref) for t, ref, on, m in coverage if not t.get("IsMultiRegionTrail") and t.get("HomeRegion", scan.region) == scan.region]
            if multi:
                commands.append(["cloudtrail", "start-logging", "--name", multi[0][1]] + scan.region_args())
            elif single:
                commands.append(["cloudtrail", "update-trail", "--name", single[0][1], "--is-multi-region-trail"] + scan.region_args())
                commands.append(["cloudtrail", "start-logging", "--name", single[0][1]] + scan.region_args())
            scan.add("cloudtrail-no-multi-region", f"arn:aws:cloudtrail:{scan.region}:{scan.account}:account", "account", evidence,
                     "Keep one multi-Region trail that logs read and write management events and is always logging; without it there is no record of who did what.", commands)
    recorders = scan.read("configservice", "describe-configuration-recorders")
    statuses = scan.read("configservice", "describe-configuration-recorder-status")
    if recorders is not None and statuses is not None:
        recording = {s.get("name") for s in statuses.get("ConfigurationRecordersStatus") or [] if s.get("recording")}
        complete = [r for r in recorders.get("ConfigurationRecorders") or [] if r.get("name") in recording and (r.get("recordingGroup") or {}).get("allSupported", True)]
        if not complete:
            listed = [f"{r.get('name')}: recording={str(r.get('name') in recording).lower()}, allSupported={str((r.get('recordingGroup') or {}).get('allSupported', True)).lower()}" for r in recorders.get("ConfigurationRecorders") or []]
            scan.add("config-not-recording", f"arn:aws:config:{scan.region}:{scan.account}:recorder", "account", listed or [f"no configuration recorder in {scan.region}"],
                     "aws configservice put-configuration-recorder (allSupported=true), put-delivery-channel, then start-configuration-recorder; Config is the history that Security Hub's checks run on.")
    keys = scan.read("kms", "list-keys")
    aliases = scan.read("kms", "list-aliases")
    names = {a.get("TargetKeyId"): a.get("AliasName", "").replace("alias/", "", 1) for a in (aliases or {}).get("Aliases") or [] if a.get("TargetKeyId")}
    for key in (keys or {}).get("Keys") or []:
        kid = key.get("KeyId", "")
        meta = (scan.read("kms", "describe-key", "--key-id", kid) or {}).get("KeyMetadata") or {}
        if not rotatable(meta) or names.get(kid, "").startswith("aws/"):
            continue
        rotation = scan.read("kms", "get-key-rotation-status", "--key-id", kid)
        if rotation is not None and not rotation.get("KeyRotationEnabled"):
            scan.add("kms-rotation-off", key.get("KeyArn", kid), names.get(kid, kid), [f"{kid}: KeyRotationEnabled=false"],
                     "Turn on automatic rotation; KMS keeps the old key material, so existing ciphertext still decrypts.", [["kms", "enable-key-rotation", "--key-id", kid] + scan.region_args()])


def scan_bundle(bundle: dict, settings: Settings) -> Scan:
    scan = Scan(bundle, settings)
    check_s3(scan)
    check_network(scan)
    check_compute(scan)
    rows = None
    report = scan.read("iam", "get-credential-report")
    try:
        rows = credential_rows(report)
    except (ValueError, UnicodeDecodeError):
        scan.gaps.append({"call": "iam get-credential-report", "reason": "Content is not base64 CSV"})
    if report is not None and rows is None:
        scan.gaps.append({"call": "iam get-credential-report", "reason": "empty report"})
    check_root(scan, rows)
    check_users(scan, rows)
    check_password_policy(scan)
    check_logging(scan)
    scan.findings.sort(key=lambda f: (RANK[f.severity], f.check.id, f.name, f.resource))
    return scan


def resolve_now(value: str, bundle: dict) -> datetime:
    base = parse_time(bundle.get("collected_at")) or utcnow()
    if not value:
        return base
    match = RELATIVE.match(value)
    if match:
        return base + timedelta(days=int(match.group(1)))
    moment = parse_time(value)
    if moment is None:
        raise PostureError(f"--now must be a date, an ISO timestamp or +Nd (days after collection), got {value!r}")
    return moment


def counts(findings: Sequence[Finding]) -> Dict[str, int]:
    return {severity: sum(1 for f in findings if f.severity == severity) for severity in SEVERITIES}


def render_text(scan: Scan, bundle: dict) -> str:
    benchmark = scan.settings.benchmark
    lines = [f"posture: account {scan.account or '?'} in {scan.region or '?'}, collected {bundle.get('collected_at')}, evaluated {iso(scan.settings.now)}",
             f"sections are CIS AWS Foundations Benchmark v{benchmark}; Security Hub CSPM controls in brackets", ""]
    for finding in scan.findings:
        section = finding.check.cis(benchmark) or "-"
        hub = f" [{finding.check.securityhub}]" if finding.check.securityhub else ""
        lines.append(f"{finding.severity.upper():<9}{finding.check.id:<28}{finding.name:<26}CIS {section}{hub}")
        lines.append(f"  {finding.check.title}")
        lines.extend(f"  evidence: {line}" for line in finding.evidence)
        lines.append(f"  fix: {finding.fix}")
        lines.extend(f"  run: aws {shlex.join(command)}" for command in finding.commands)
    totals = counts(scan.findings)
    lines.append("")
    lines.append(f"{len(scan.findings)} finding(s): " + ", ".join(f"{totals[s]} {s}" for s in SEVERITIES))
    if scan.gaps:
        lines.append(f"{len(scan.gaps)} gap(s): these calls failed or were not collected, so some checks did not run")
        lines.extend(f"  {gap['call']}: {gap['reason']}" for gap in scan.gaps)
    return "\n".join(lines)


def render_markdown(scan: Scan) -> str:
    benchmark = scan.settings.benchmark
    rows = ["| Severity | Check | Resource | CIS v" + benchmark + " | Security Hub | Evidence |", "|---|---|---|---|---|---|"]
    for f in scan.findings:
        evidence = "; ".join(f.evidence).replace("|", "\\|")
        rows.append(f"| {f.severity} | `{f.check.id}` | `{f.name}` | {f.check.cis(benchmark) or '-'} | {f.check.securityhub or '-'} | {evidence} |")
    return "\n".join(rows)


def render_checks(benchmark: str) -> str:
    lines = [f"{'CHECK':<28}{'SEVERITY':<10}{'CIS v' + benchmark:<12}{'SECURITY HUB':<16}TITLE"]
    for check in CHECKS.values():
        lines.append(f"{check.id:<28}{check.severity:<10}{check.cis(benchmark) or '-':<12}{check.securityhub or '-':<16}{check.title}")
    return "\n".join(lines)


def read_bundle(path: str) -> dict:
    try:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as handle:
            return json.load(handle)
    except OSError as exc:
        raise PostureError(f"cannot read {path}: {exc.strerror}") from exc
    except ValueError as exc:
        raise PostureError(f"{path} is not JSON: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="posture", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    col = sub.add_parser("collect", help="run the read-only AWS CLI calls and save their outputs as one bundle")
    col.add_argument("-o", "--output", default="-", help="bundle file (default stdout)")
    col.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
    col.add_argument("--endpoint-url", default="", help="send every call to an emulator such as moto")
    col.add_argument("--aws", default="aws", help="path to the AWS CLI")
    scan = sub.add_parser("scan", help="evaluate a bundle and print findings")
    scan.add_argument("bundle", help="bundle from posture collect (- for stdin)")
    scan.add_argument("--now", default="", help="evaluation time: a date, an ISO timestamp, or +Nd after collection (default: the collection time)")
    scan.add_argument("--benchmark", choices=BENCHMARKS, default="5.0.0", help="which CIS AWS Foundations Benchmark numbering to print")
    scan.add_argument("--fail-on", choices=SEVERITIES + ("none",), default="high", help="exit 1 when a finding is at least this severe (default high)")
    scan.add_argument("--min-severity", choices=SEVERITIES, default="low", help="hide findings below this severity")
    scan.add_argument("--key-age-days", type=int, default=90)
    scan.add_argument("--unused-days", type=int, default=45)
    scan.add_argument("--root-used-days", type=int, default=90)
    form = scan.add_mutually_exclusive_group()
    form.add_argument("--json", action="store_true", help="machine-readable output")
    form.add_argument("--markdown", action="store_true", help="a table for a pull request or a review")
    checks = sub.add_parser("checks", help="list the checks with their CIS sections and Security Hub controls")
    checks.add_argument("--benchmark", choices=BENCHMARKS, default="5.0.0")
    checks.add_argument("--json", action="store_true")
    return parser


def run_scan(args: argparse.Namespace) -> int:
    bundle = read_bundle(args.bundle)
    settings = Settings(resolve_now("", bundle), args.benchmark, args.key_age_days, args.unused_days, args.root_used_days)
    settings.now = resolve_now(args.now, bundle)
    scan = scan_bundle(bundle, settings)
    scan.findings = [f for f in scan.findings if RANK[f.severity] <= RANK[args.min_severity]]
    failing = [f for f in scan.findings if args.fail_on != "none" and RANK[f.severity] <= RANK[args.fail_on]]
    if args.json:
        print(json.dumps({"account": scan.account, "region": scan.region, "collected_at": bundle.get("collected_at"), "evaluated_at": iso(settings.now),
                          "benchmark": f"CIS AWS Foundations Benchmark v{settings.benchmark}", "counts": counts(scan.findings),
                          "findings": [f.to_json() for f in scan.findings], "gaps": scan.gaps}, indent=2))
    elif args.markdown:
        print(render_markdown(scan))
    else:
        print(render_text(scan, bundle))
    return 1 if failing else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "checks":
            print(json.dumps([c.to_json() for c in CHECKS.values()], indent=2) if args.json else render_checks(args.benchmark))
            return 0
        if args.command == "collect":
            bundle = collect(aws_cli(args.endpoint_url, args.region, args.aws), args.region)
            identity = bundle["calls"]["sts get-caller-identity"]
            text = json.dumps(bundle, indent=2)
            if args.output == "-":
                print(text)
            else:
                with open(args.output, "w", encoding="utf-8") as handle:
                    handle.write(text + "\n")
            failed = sorted(key for key, call in bundle["calls"].items() if not call.get("ok"))
            print(f"posture: {len(bundle['calls'])} calls, {len(failed)} returned an error (kept in the bundle; absent configuration is an error too)", file=sys.stderr)
            if not identity.get("ok"):
                print(f"posture: sts get-caller-identity failed ({identity.get('error')}): check credentials and --endpoint-url", file=sys.stderr)
                return 2
            return 0
        return run_scan(args)
    except PostureError as exc:
        print(f"posture: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
