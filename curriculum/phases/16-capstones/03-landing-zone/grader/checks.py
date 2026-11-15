"""Milestone checks for capstones/landing-zone.

One function per milestone in ../lesson.json: milestone `some-id` is checked by
`check_some_id(project)`, which gets the project folder and returns
(passed, detail). The grader drives the project only through its interface:
`bin/lz` (plan, apply, policy, drift), the module folders and OpenTofu. Every
check works on a throwaway copy of the project and a freshly reset moto, so
checks do not depend on each other. Python 3.9+, standard library only.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import lz_aws  # noqa: E402
import lz_env  # noqa: E402
import lz_hcl  # noqa: E402
import lz_iam  # noqa: E402

Result = Tuple[bool, str]

ENVS = ("hub", "dev", "prod")
SPOKES = ("dev", "prod")
AWS_MODULES = ("network", "identity", "baseline")
CLOUD_MODULES = {"network": "aws", "network-gcp": "google", "network-azure": "azurerm"}
NETWORK_INPUTS = ("name", "env", "region", "cidr_block", "zones", "tags", "flow_log_destination")
NETWORK_OUTPUTS = ("network_id", "cidr_block", "private_subnet_ids", "private_subnet_cidrs", "public_subnet_ids", "public_subnet_cidrs", "public_ingress_ports")
ALLOCATION = ipaddress.ip_network("10.0.0.0/8")
HOME_REGION = lz_aws.REGION
OTHER_REGION = "us-east-1"
LB_TAG = ("role", "load-balancer")
WORLD = ("0.0.0.0/0", "::/0")
FIXTURES = os.path.join(HERE, "fixtures")
TEST_VALUES = {
    "aws": ("eu-central-1", ["eu-central-1a", "eu-central-1b", "eu-central-1c"], "arn:aws:s3:::cc-grade-flow-logs"),
    "google": ("europe-west3", ["europe-west3-a", "europe-west3-b", "europe-west3-c"], "cc-grade-flow-logs"),
    "azurerm": ("germanywestcentral", ["1", "2", "3"], "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/cc-grade/providers/Microsoft.Storage/storageAccounts/ccgradeflowlogs"),
}
TIMEOUT_INIT_APPLY = 900.0
TIMEOUT_PLAN = 600.0
TIMEOUT_LOCKED = 180.0


class Tools:
    def __init__(self, runner: lz_env.Runner = lz_env.run, moto: Optional[Callable[[], Any]] = None, client: Callable[[str], Any] = lz_aws.Client,
                 which: Callable[[str], Optional[str]] = shutil.which, workspace: Callable[[str], Any] = lz_env.Workspace) -> None:
        self.runner = runner
        self.moto = moto or lz_env.shared_moto
        self.client = client
        self.which = which
        self.workspace = workspace


def _on_sigterm(signum: int, frame: Any) -> None:
    raise SystemExit(128 + signum)


if threading.current_thread() is threading.main_thread():
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass


def lz_path(project: str) -> str:
    return os.path.join(project, "bin", "lz")


def missing_lz(project: str) -> Optional[str]:
    path = lz_path(project)
    if not os.path.isfile(path):
        return "bin/lz is missing: add an executable bin/lz with the subcommands plan, apply, policy and drift (starter/bin/lz is a stub to start from)"
    if not os.access(path, os.X_OK):
        return "bin/lz is not executable: chmod +x bin/lz and commit the mode (git update-index --chmod=+x bin/lz)"
    return None


def missing_tofu(tools: Tools) -> Optional[str]:
    if not tools.which("tofu"):
        return "OpenTofu is not installed: install tofu 1.10 or newer (https://opentofu.org/docs/intro/install/) so the grader can run it"
    return None


def missing_envs(project: str, envs: Sequence[str] = ENVS) -> Optional[str]:
    absent = [env for env in envs if not os.path.isdir(os.path.join(project, "envs", env))]
    if absent:
        return f"envs/{', envs/'.join(absent)} missing: the landing zone has a hub and the spokes dev and prod, one root module per folder"
    return None


def preflight(project: str, tools: Tools, envs: Sequence[str] = ENVS) -> Optional[str]:
    return missing_lz(project) or missing_tofu(tools) or missing_envs(project, envs)


def lz(tools: Tools, cwd: str, args: Sequence[str], env: Dict[str, str], timeout: float) -> lz_env.Result:
    return tools.runner([os.path.join(cwd, "bin", "lz")] + list(args), cwd, env, timeout)


def describe(args: Sequence[str], result: lz_env.Result) -> str:
    return f"`bin/lz {' '.join(args)}` failed (exit {result.code}): {result.tail()}"


def start_moto(tools: Tools) -> Tuple[Optional[str], str]:
    try:
        moto = tools.moto()
        endpoint = moto.start()
        moto.reset()
    except lz_env.SetupError as error:
        return None, str(error)
    except (lz_aws.AwsError, OSError) as error:
        return None, f"could not reset moto: {error}"
    return endpoint, ""


def apply_envs(tools: Tools, cwd: str, env: Dict[str, str], waves: Sequence[Sequence[str]]) -> Optional[str]:
    for wave in waves:
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            results = list(pool.map(lambda name: (name, lz(tools, cwd, ["apply", name], env, TIMEOUT_INIT_APPLY)), wave))
        for name, result in results:
            if result.code != 0:
                return describe(["apply", name], result)
    return None


def variable_problems(path: str, label: str) -> Tuple[List[str], Dict[str, int]]:
    try:
        files = lz_hcl.parse_dir(path)
    except lz_hcl.HclError as error:
        return [f"{label}: cannot read HCL: {error}"], {}
    problems: List[str] = []
    variables = lz_hcl.top_blocks(files, "variable")
    outputs = lz_hcl.top_blocks(files, "output")
    if not files:
        return [f"{label} has no .tf files"], {}
    if not variables:
        problems.append(f"{label} declares no variables: a module takes its settings as typed, described variables")
    for block in variables:
        name = block.labels[0] if block.labels else "?"
        for attribute in ("type", "description"):
            if attribute not in block.attributes:
                problems.append(f'{label}: variable "{name}" has no {attribute}')
    validations = sum(len(block.children("validation")) for block in variables)
    if variables and not validations:
        problems.append(f"{label}: no variable has a validation block; reject bad input (a CIDR outside 10.0.0.0/8, a missing tag) before plan")
    if not outputs:
        problems.append(f"{label} declares no outputs")
    for block in outputs:
        if "description" not in block.attributes:
            problems.append(f'{label}: output "{block.labels[0] if block.labels else "?"}" has no description')
    return problems, {"variables": len(variables), "validations": validations, "outputs": len(outputs)}


def tofu_dirs(project: str) -> List[str]:
    found = []
    for parent in ("modules", "envs"):
        root = os.path.join(project, parent)
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                if os.path.isdir(os.path.join(root, name)) and any(f.endswith(".tf") for f in os.listdir(os.path.join(root, name))):
                    found.append(f"{parent}/{name}")
    return found


def validate_dir(tools: Tools, copy: str, relative: str, env: Dict[str, str]) -> Optional[str]:
    cwd = os.path.join(copy, relative)
    init = tools.runner(["tofu", "init", "-backend=false", "-input=false", "-no-color"], cwd, env, TIMEOUT_INIT_APPLY)
    if init.code != 0:
        return f"{relative}: tofu init -backend=false failed: {init.tail(6)}"
    done = tools.runner(["tofu", "validate", "-json", "-no-color"], cwd, env, 300)
    report = lz_env.parse_json_output(done.out)
    if not isinstance(report, dict):
        return f"{relative}: tofu validate -json printed no JSON (exit {done.code}): {done.tail(6)}"
    if not report.get("valid"):
        errors = [d for d in report.get("diagnostics") or [] if d.get("severity") == "error"]
        first = errors[0] if errors else {}
        where = (first.get("range") or {}).get("filename", "")
        line = ((first.get("range") or {}).get("start") or {}).get("line", "")
        return f"{relative}: tofu validate: {first.get('summary', 'invalid')} {first.get('detail', '')} ({where}:{line})".strip()
    return None


def check_module_structure(project: str, tools: Optional[Tools] = None) -> Result:
    tools = tools or Tools()
    absent = [m for m in AWS_MODULES if not os.path.isdir(os.path.join(project, "modules", m))]
    if absent:
        return False, f"modules/{', modules/'.join(absent)} missing: create one folder per module with variables.tf, main.tf and outputs.tf"
    problems: List[str] = []
    totals = {"variables": 0, "validations": 0, "outputs": 0}
    for module in AWS_MODULES:
        found, counts = variable_problems(os.path.join(project, "modules", module), f"modules/{module}")
        problems += found
        for key, value in counts.items():
            totals[key] += value
    if problems:
        return False, "; ".join(problems[:5]) + (f" (and {len(problems) - 5} more)" if len(problems) > 5 else "")
    blocker = missing_envs(project) or missing_tofu(tools)
    if blocker:
        return False, blocker
    dirs = tofu_dirs(project)
    with tools.workspace(project) as workspace:
        copy = workspace.copy()
        env = lz_env.tofu_env()
        unformatted = []
        for parent in ("modules", "envs"):
            done = tools.runner(["tofu", "fmt", "-check", "-recursive", "-list=true", "-no-color", parent], copy, env, 120)
            if done.code != 0:
                unformatted += [line.strip() for line in done.out.splitlines() if line.strip()] or [f"{parent} ({done.tail(3)})"]
        if unformatted:
            return False, f"tofu fmt -check: {', '.join(unformatted[:6])} not formatted; run `tofu fmt -recursive`"
        first = validate_dir(tools, copy, dirs[0], env) if dirs else None
        with ThreadPoolExecutor(max_workers=4) as pool:
            failures = [f for f in [first] + list(pool.map(lambda d: validate_dir(tools, copy, d, env), dirs[1:])) if f]
    if failures:
        return False, "; ".join(failures[:3])
    return True, f"{len(dirs)} module and env folders pass fmt and validate; {totals['variables']} typed, described variables, {totals['validations']} validation blocks, {totals['outputs']} outputs"


def private_subnets(subnets: List[Dict[str, Any]], tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    main = next((t for t in tables if any(str(a.get("main")).lower() == "true" for a in lz_aws.as_list(t.get("associationSet")))), None)

    def table_for(subnet_id: str) -> Optional[Dict[str, Any]]:
        for table in tables:
            if any(a.get("subnetId") == subnet_id for a in lz_aws.as_list(table.get("associationSet"))):
                return table
        return main

    def internet_route(table: Optional[Dict[str, Any]]) -> bool:
        routes = lz_aws.as_list((table or {}).get("routeSet"))
        return any(r.get("destinationCidrBlock") in WORLD and str(r.get("gatewayId") or "").startswith("igw-") for r in routes)

    return [s for s in subnets if not internet_route(table_for(s.get("subnetId", ""))) and str(s.get("mapPublicIpOnLaunch")).lower() != "true"]


def open_ingress(groups: List[Dict[str, Any]]) -> List[str]:
    found = []
    for group in groups:
        tags = lz_aws.tag_map(group.get("tagSet"))
        for permission in lz_aws.as_list(group.get("ipPermissions")):
            sources = [r.get("cidrIp") for r in lz_aws.as_list(permission.get("ipRanges"))] + [r.get("cidrIpv6") for r in lz_aws.as_list(permission.get("ipv6Ranges"))]
            if not any(s in WORLD for s in sources):
                continue
            protocol = str(permission.get("ipProtocol"))
            low, high = str(permission.get("fromPort")), str(permission.get("toPort"))
            allowed = protocol in ("tcp", "6") and low == high == "443" and tags.get(LB_TAG[0]) == LB_TAG[1]
            if not allowed:
                found.append(f"security group {group.get('groupName')} ({group.get('groupId')}) allows {protocol} {low}-{high} from the internet")
    return found


def network_problems(client: Any) -> Tuple[List[str], str]:
    problems: List[str] = []
    vpcs: Dict[str, List[Dict[str, Any]]] = {}
    for vpc in client.ec2_items("DescribeVpcs", "vpcSet"):
        env = lz_aws.tag_map(vpc.get("tagSet")).get("env")
        if env in ENVS:
            vpcs.setdefault(env, []).append(vpc)
    for env in ENVS:
        if not vpcs.get(env):
            problems.append(f"no VPC tagged env={env} after `bin/lz apply {env}`: tag every resource with env, owner and cost-center")
        elif len(vpcs[env]) > 1:
            problems.append(f"{len(vpcs[env])} VPCs tagged env={env}; expected exactly one")
    if problems:
        return problems, ""
    chosen = {env: vpcs[env][0] for env in ENVS}
    networks = {}
    for env, vpc in chosen.items():
        network = ipaddress.ip_network(str(vpc.get("cidrBlock")), strict=False)
        networks[env] = network
        if not network.subnet_of(ALLOCATION):  # type: ignore[arg-type]
            problems.append(f"{env} VPC {network} is outside the allocation {ALLOCATION}")
    names = list(ENVS)
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            if networks[left].overlaps(networks[right]):
                problems.append(f"{left} {networks[left]} overlaps {right} {networks[right]}: peered networks need distinct ranges")
    tables_by_env: Dict[str, List[Dict[str, Any]]] = {}
    privates_by_env: Dict[str, List[Dict[str, Any]]] = {}
    for env, vpc in chosen.items():
        vpc_id = vpc.get("vpcId", "")
        subnets = client.ec2_items("DescribeSubnets", "subnetSet", {"vpc-id": vpc_id})
        tables = client.ec2_items("DescribeRouteTables", "routeTableSet", {"vpc-id": vpc_id})
        tables_by_env[env] = tables
        private = private_subnets(subnets, tables)
        privates_by_env[env] = private
        zones = {s.get("availabilityZone") for s in private}
        if len(private) < 2 or len(zones) < 2:
            problems.append(f"{env}: {len(private)} private subnet(s) in {len(zones)} zone(s); need private subnets (no route to an internet gateway, no public IPs) in at least two zones")
        groups = client.ec2_items("DescribeSecurityGroups", "securityGroupInfo", {"vpc-id": vpc_id})
        problems += [f"{env}: {p}; only tcp 443 on a group tagged {LB_TAG[0]}={LB_TAG[1]} may be public" for p in open_ingress(groups)]
        logs = [log for log in client.ec2_items("DescribeFlowLogs", "flowLogSet") if log.get("resourceId") == vpc_id]
        if not logs:
            problems.append(f"{env}: no flow log on VPC {vpc_id}; add aws_flow_log with traffic_type ALL")
    peerings = client.ec2_items("DescribeVpcPeeringConnections", "vpcPeeringConnectionSet")
    hub_id = chosen["hub"].get("vpcId")
    for spoke in SPOKES:
        spoke_id = chosen[spoke].get("vpcId")
        link = next((p for p in peerings
                     if {(p.get("requesterVpcInfo") or {}).get("vpcId"), (p.get("accepterVpcInfo") or {}).get("vpcId")} == {hub_id, spoke_id}
                     and ((p.get("status") or {}).get("code")) == "active"), None)
        if link is None:
            problems.append(f"no active peering between hub and {spoke}: the hub peers with every spoke that exists when it is applied (the grader applies dev and prod, then hub)")
            continue
        pcx = link.get("vpcPeeringConnectionId")
        routed = set()
        for table in tables_by_env[spoke]:
            if any(r.get("vpcPeeringConnectionId") == pcx and r.get("destinationCidrBlock") == str(networks["hub"]) for r in lz_aws.as_list(table.get("routeSet"))):
                routed.add(table.get("routeTableId"))
        if not routed:
            problems.append(f"{spoke}: no route to the hub range {networks['hub']} through {pcx}")
        if not any(r.get("vpcPeeringConnectionId") == pcx and r.get("destinationCidrBlock") == str(networks[spoke]) for t in tables_by_env["hub"] for r in lz_aws.as_list(t.get("routeSet"))):
            problems.append(f"hub: no route to the {spoke} range {networks[spoke]} through {pcx}")
    summary = ", ".join(f"{env} {networks[env]} ({len(privates_by_env[env])} private subnets)" for env in ENVS)
    return problems, summary


def check_network_baseline(project: str, tools: Optional[Tools] = None) -> Result:
    tools = tools or Tools()
    blocker = preflight(project, tools)
    if blocker:
        return False, blocker
    endpoint, error = start_moto(tools)
    if not endpoint:
        return False, error
    env = lz_env.aws_env(endpoint)
    with tools.workspace(project) as workspace:
        copy = workspace.copy()
        failed = apply_envs(tools, copy, env, [list(SPOKES), ["hub"]])
        if failed:
            return False, failed
        problems, summary = network_problems(tools.client(endpoint))
    if problems:
        return False, "; ".join(problems[:4]) + (f" (and {len(problems) - 4} more)" if len(problems) > 4 else "")
    return True, f"{summary}; no open ingress except 443 on load balancers; flow logs on; hub peered with dev and prod"


def role_policies(client: Any, role_name: str) -> Tuple[Dict[str, Any], lz_iam.RolePolicies]:
    role = (client.iam("GetRole", {"RoleName": role_name}).get("Role")) or {}
    policies = lz_iam.RolePolicies(role_name)

    def managed(arn: str) -> Dict[str, Any]:
        version = (client.iam("GetPolicy", {"PolicyArn": arn}).get("Policy") or {}).get("DefaultVersionId")
        document = (client.iam("GetPolicyVersion", {"PolicyArn": arn, "VersionId": str(version)}).get("PolicyVersion") or {}).get("Document")
        return lz_iam.decode_document(document)

    for attached in lz_aws.as_list(client.iam("ListAttachedRolePolicies", {"RoleName": role_name}).get("AttachedPolicies")):
        policies.identity.append((str(attached.get("PolicyName")), managed(str(attached.get("PolicyArn")))))
    for name in lz_aws.as_list(client.iam("ListRolePolicies", {"RoleName": role_name}).get("PolicyNames")):
        document = client.iam("GetRolePolicy", {"RoleName": role_name, "PolicyName": str(name)}).get("PolicyDocument")
        policies.identity.append((f"inline {name}", lz_iam.decode_document(document)))
    boundary = (role.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn")
    if boundary:
        policies.boundary = (f"boundary {str(boundary).rsplit('/', 1)[-1]}", managed(str(boundary)))
    return role, policies


EXPECTATIONS = (
    ("deploy", "ec2:CreateVpc", "*", HOME_REGION, "allowed", "the deploy role must be able to manage networks in the home region"),
    ("deploy", "ec2:CreateVpc", "*", OTHER_REGION, "explicitDeny", "an explicit Deny on aws:RequestedRegion must stop work outside the home region"),
    ("deploy", "iam:CreateUser", "*", OTHER_REGION, "denied", "the deploy role must not create IAM users"),
    ("deploy", "iam:DeleteRolePermissionsBoundary", "arn:aws:iam::123456789012:role/lz-dev-deploy", OTHER_REGION, "denied", "no role may remove its own permissions boundary"),
    ("deploy", "organizations:LeaveOrganization", "*", HOME_REGION, "explicitDeny", "account-level actions need an explicit Deny (checked from the home region, so the region guardrail is not what stops them)"),
    ("deploy", "account:PutAlternateContact", "*", HOME_REGION, "explicitDeny", "account-level actions need an explicit Deny"),
    ("readonly", "ec2:DescribeVpcs", "*", HOME_REGION, "allowed", "the read-only role must be able to describe networks"),
    ("readonly", "iam:GetRole", "*", OTHER_REGION, "allowed", "global services such as IAM answer from us-east-1, so the region guardrail must exempt them"),
    ("readonly", "ec2:CreateVpc", "*", HOME_REGION, "denied", "the read-only role must not create anything"),
    ("readonly", "s3:PutObject", "arn:aws:s3:::lz-dev-log-archive/x", HOME_REGION, "denied", "the read-only role must not write objects"),
    ("readonly", "organizations:LeaveOrganization", "*", HOME_REGION, "explicitDeny", "the guardrail applies to every role"),
)


def identity_problems(client: Any, env: str = "dev") -> Tuple[List[str], str]:
    names: List[str] = []
    marker: Dict[str, str] = {}
    while True:
        page = client.iam("ListRoles", marker)
        names += [str(r.get("RoleName")) for r in lz_aws.as_list(page.get("Roles")) if isinstance(r, dict)]
        if str(page.get("IsTruncated")).lower() != "true" or not page.get("Marker"):
            break
        marker = {"Marker": str(page.get("Marker"))}
    prefix = f"lz-{env}-"
    problems: List[str] = []
    wanted = {"deploy": prefix + "deploy", "readonly": prefix + "readonly"}
    absent = [name for name in wanted.values() if name not in names]
    if absent:
        return [f"role(s) {', '.join(absent)} missing after `bin/lz apply {env}`: the identity module creates <name>-deploy and <name>-readonly with name lz-{env}"], ""
    loaded: Dict[str, lz_iam.RolePolicies] = {}
    checked = 0
    for name in names:
        role, policies = role_policies(client, name)
        tags = {str(t.get("Key")): str(t.get("Value")) for t in lz_aws.as_list(role.get("Tags")) if isinstance(t, dict)}
        if not name.startswith(prefix) and tags.get("env") != env:
            continue
        checked += 1
        loaded[name] = policies
        if policies.boundary is None:
            problems.append(f"role {name} has no permissions boundary")
        problems += [f"role {name}: {broad}" for broad in lz_iam.broad_allows(policies.identity + ([policies.boundary] if policies.boundary else []))]
    for access, action, resource, region, expected, why in EXPECTATIONS:
        role_name = wanted[access]
        try:
            decision = lz_iam.evaluate(loaded[role_name], action, resource.replace("lz-dev-", prefix), {"aws:RequestedRegion": region})
        except lz_iam.PolicyError as error:
            problems.append(f"{role_name}: cannot evaluate its policies: {error}")
            continue
        ok = decision.allowed if expected == "allowed" else (decision.decision == "explicitDeny" if expected == "explicitDeny" else not decision.allowed)
        if not ok:
            problems.append(f"{role_name} {action} in {region}: got {decision.decision} ({decision.policy} {decision.sid}), want {expected}: {why}")
    return problems, f"{checked} roles with boundaries, {len(EXPECTATIONS)} requests decided as expected"


def check_identity_guardrails(project: str, tools: Optional[Tools] = None) -> Result:
    tools = tools or Tools()
    blocker = preflight(project, tools, ("dev",))
    if blocker:
        return False, blocker
    endpoint, error = start_moto(tools)
    if not endpoint:
        return False, error
    with tools.workspace(project) as workspace:
        copy = workspace.copy()
        failed = apply_envs(tools, copy, lz_env.aws_env(endpoint), [["dev"]])
        if failed:
            return False, failed
        problems, summary = identity_problems(tools.client(endpoint))
    if problems:
        return False, "; ".join(problems[:4]) + (f" (and {len(problems) - 4} more)" if len(problems) > 4 else "")
    return True, summary


def load_policy_cases() -> List[Dict[str, Any]]:
    with open(os.path.join(FIXTURES, "plans", "cases.json"), encoding="utf-8") as handle:
        return json.load(handle)["cases"]


def violations_of(report: Any) -> Optional[List[Tuple[str, str]]]:
    if not isinstance(report, dict) or not isinstance(report.get("violations"), list):
        return None
    pairs = []
    for item in report["violations"]:
        if not isinstance(item, dict):
            return None
        pairs.append((str(item.get("rule")), str(item.get("address"))))
    return pairs


def policy_fixture_problems(tools: Tools, cwd: str, env: Dict[str, str]) -> Tuple[List[str], int]:
    problems: List[str] = []
    cases = load_policy_cases()
    for case in cases:
        path = os.path.join(FIXTURES, "plans", case["file"])
        args = ["policy", path, "--json"]
        done = lz(tools, cwd, args, env, 120)
        found = violations_of(lz_env.parse_json_output(done.out))
        expected = [(e["rule"], e["address"]) for e in case["expect"]]
        if found is None:
            problems.append(f"{case['file']}: `bin/lz policy <plan> --json` printed no {{\"violations\": [...]}} JSON (exit {done.code}): {done.tail(4)}")
            if done.code in (126, 127) or "not implemented" in done.err:
                break
            continue
        if not expected:
            if done.code != 0 or found:
                problems.append(f"{case['file']} ({case['why']}): expected exit 0 and no violations, got exit {done.code} with {found[:3]}")
            continue
        missing = [pair for pair in expected if pair not in found]
        if done.code != 1:
            problems.append(f"{case['file']} ({case['why']}): violations must exit 1, got {done.code}")
        elif missing:
            problems.append(f"{case['file']} ({case['why']}): expected rule {missing[0][0]} on {missing[0][1]}, got {found[:3] or 'no violations'}")
    return problems, len(cases)


def own_plan_problem(tools: Tools, cwd: str, env: Dict[str, str], name: str) -> Optional[str]:
    args = ["plan", name, "--json"]
    done = lz(tools, cwd, args, env, TIMEOUT_PLAN)
    if done.code != 0:
        return describe(args, done)
    summary = lz_env.parse_json_output(done.out)
    path = summary.get("plan_json") if isinstance(summary, dict) else None
    if not path:
        return f"`bin/lz plan {name} --json` must print {{\"plan_json\": \"<path>\"}}; got: {done.out.strip()[:200]}"
    path = path if os.path.isabs(str(path)) else os.path.join(cwd, str(path))
    try:
        with open(path, encoding="utf-8") as handle:
            plan = json.load(handle)
    except (OSError, ValueError) as error:
        return f"plan JSON {path} from `bin/lz plan {name}` is unreadable: {error}"
    types = {c.get("type") for c in plan.get("resource_changes") or [] if isinstance(c, dict)}
    if "aws_vpc" not in types:
        return f"the plan JSON of {name} has no aws_vpc change: write `tofu show -json` of a plan made against an empty moto"
    checked = lz(tools, cwd, ["policy", path, "--json"], env, 120)
    found = violations_of(lz_env.parse_json_output(checked.out))
    if checked.code != 0 or found:
        return f"your own {name} plan fails your policy (exit {checked.code}): {(found or [])[:3] or checked.tail(4)}"
    return None


def check_policy_gate(project: str, tools: Optional[Tools] = None) -> Result:
    tools = tools or Tools()
    blocker = missing_lz(project)
    if blocker:
        return False, blocker
    with tools.workspace(project) as workspace:
        copy = workspace.copy()
        problems, count = policy_fixture_problems(tools, copy, lz_env.tofu_env())
        if problems:
            return False, "; ".join(problems[:3]) + (f" (and {len(problems) - 3} more)" if len(problems) > 3 else "")
        blocker = missing_tofu(tools) or missing_envs(project)
        if blocker:
            return False, blocker
        endpoint, error = start_moto(tools)
        if not endpoint:
            return False, error
        env = lz_env.aws_env(endpoint)
        with ThreadPoolExecutor(max_workers=len(ENVS)) as pool:
            failures = [f for f in pool.map(lambda name: own_plan_problem(tools, copy, env, name), ENVS) if f]
    if failures:
        return False, "; ".join(failures)
    return True, f"{count} grader plans judged as expected; the hub, dev and prod plans pass your policy"


def render_interface_test(provider: str) -> str:
    region, zones, destination = TEST_VALUES[provider]
    with open(os.path.join(FIXTURES, "interface.tftest.hcl.tmpl"), encoding="utf-8") as handle:
        text = handle.read()
    quoted = [json.dumps(z) for z in zones]
    mock = f'mock_provider "{provider}" {{}}\n'
    if provider == "azurerm":
        with open(os.path.join(FIXTURES, "azurerm-mock.hcl"), encoding="utf-8") as handle:
            mock = handle.read()
    return (text.replace("@MOCK@\n", mock).replace("@REGION@", region).replace("@DESTINATION@", destination)
            .replace("@ZONES2@", ", ".join(quoted[:2])).replace("@ZONES3@", ", ".join(quoted)))


def interface_problems(project: str) -> List[str]:
    problems = []
    for module in CLOUD_MODULES:
        path = os.path.join(project, "modules", module)
        try:
            files = lz_hcl.parse_dir(path)
        except lz_hcl.HclError as error:
            problems.append(str(error))
            continue
        inputs = {b.labels[0] for b in lz_hcl.top_blocks(files, "variable") if b.labels}
        outputs = {b.labels[0] for b in lz_hcl.top_blocks(files, "output") if b.labels}
        lacking = [f"variable {v}" for v in NETWORK_INPUTS if v not in inputs] + [f"output {o}" for o in NETWORK_OUTPUTS if o not in outputs]
        if lacking:
            problems.append(f"modules/{module} lacks {', '.join(lacking[:5])}: every network module takes {', '.join(NETWORK_INPUTS)} and returns {', '.join(NETWORK_OUTPUTS)}")
    return problems


def learner_tests(project: str, module: str) -> Tuple[int, List[str], Optional[str]]:
    folder = os.path.join(project, "modules", module, "tests")
    if not os.path.isdir(folder):
        return 0, [], f"modules/{module}/tests/ is missing: add *.tftest.hcl files with mock_provider and at least 3 run blocks"
    runs = 0
    mocks: List[str] = []
    for name in sorted(os.listdir(folder)):
        if name.endswith(".tftest.hcl"):
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as handle:
                    root = lz_hcl.parse(handle.read())
            except lz_hcl.HclError as error:
                return 0, [], f"modules/{module}/tests/{name}: {error}"
            runs += len(root.children("run"))
            mocks += [b.labels[0] for b in root.children("mock_provider") if b.labels]
    return runs, mocks, None


def check_multi_cloud(project: str, tools: Optional[Tools] = None) -> Result:
    tools = tools or Tools()
    absent = [m for m in CLOUD_MODULES if not os.path.isdir(os.path.join(project, "modules", m))]
    if absent:
        return False, f"modules/{', modules/'.join(absent)} missing: build the GCP and Azure networks with the same inputs and outputs as modules/network"
    problems = interface_problems(project)
    for module, provider in CLOUD_MODULES.items():
        if module == "network":
            continue
        runs, mocks, error = learner_tests(project, module)
        if error:
            problems.append(error)
        elif runs < 3:
            problems.append(f"modules/{module}/tests has {runs} run block(s); write at least 3 (subnet math, no public ingress, a rejected input)")
        elif provider not in mocks:
            problems.append(f'modules/{module}/tests never declares mock_provider "{provider}": tests must run offline, with no cloud account')
    if problems:
        return False, "; ".join(problems[:3])
    blocker = missing_tofu(tools)
    if blocker:
        return False, blocker
    env = lz_env.tofu_env()
    for key in [k for k in env if k.startswith(("AWS_", "GOOGLE_", "ARM_", "AZURE_", "CLOUDSDK_"))]:
        del env[key]
    with tools.workspace(project) as workspace:
        copy = workspace.copy()

        def run_module(item: Tuple[str, str]) -> Optional[str]:
            module, provider = item
            cwd = os.path.join(copy, "modules", module)
            os.makedirs(os.path.join(cwd, "tests"), exist_ok=True)
            with open(os.path.join(cwd, "tests", "cc_grader_interface.tftest.hcl"), "w", encoding="utf-8") as handle:
                handle.write(render_interface_test(provider))
            init = tools.runner(["tofu", "init", "-backend=false", "-input=false", "-no-color"], cwd, env, TIMEOUT_INIT_APPLY)
            if init.code != 0:
                return f"modules/{module}: tofu init failed: {init.tail(5)}"
            done = tools.runner(["tofu", "test", "-no-color"], cwd, env, TIMEOUT_PLAN)
            if done.code != 0:
                lines = [line.strip(" │╷╵") for line in (done.out + "\n" + done.err).splitlines()]
                failing = [line for line in lines if line.endswith("... fail") or line.startswith(("grader:", "Error:"))]
                return f"modules/{module}: tofu test failed: {' | '.join(failing[:5]) or done.tail(5)}"
            return None

        with ThreadPoolExecutor(max_workers=3) as pool:
            failures = [f for f in pool.map(run_module, CLOUD_MODULES.items()) if f]
    if failures:
        return False, "; ".join(failures)
    return True, "network, network-gcp and network-azure share one interface; your tests and the grader's interface test pass with mock providers"


def lock_present(client: Any) -> bool:
    for bucket in client.s3_buckets():
        if any(key.endswith(".tflock") for key in client.s3_keys(bucket)):
            return True
    try:
        tables = client.dynamodb("ListTables", {}).get("TableNames") or []
    except lz_aws.AwsError:
        return False
    for table in tables:
        items = client.dynamodb("Scan", {"TableName": table}).get("Items") or []
        if any("Info" in item for item in items if isinstance(item, dict)):
            return True
    return False


def read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def lock_problem(tools: Tools, first: str, second: str, env: Dict[str, str], client: Any) -> Optional[str]:
    args = ["plan", "dev", "--json"]
    logs = os.path.dirname(first)
    for attempt in range(3):
        out_path, err_path = os.path.join(logs, f"first-{attempt}.out"), os.path.join(logs, f"first-{attempt}.err")
        with open(out_path, "w") as out_file, open(err_path, "w") as err_file:
            process = subprocess.Popen([os.path.join(first, "bin", "lz")] + args, cwd=first, env=env, stdout=out_file, stderr=err_file, start_new_session=True)
        stopped = False
        try:
            lz_env.wait_until(lambda: process.poll() is not None or lock_present(client), TIMEOUT_PLAN, 0.05)
            if process.poll() is None and lock_present(client):
                os.killpg(process.pid, signal.SIGSTOP)
                stopped = True
            if not stopped or not lock_present(client):
                if stopped:
                    os.killpg(process.pid, signal.SIGCONT)
                    stopped = False
                process.wait(timeout=TIMEOUT_PLAN)
                if process.returncode != 0:
                    return describe(args, lz_env.Result(process.returncode, read_text(out_path), read_text(err_path)))
                if attempt == 2:
                    return "never saw a state lock while `bin/lz plan dev` ran: enable locking in the S3 backend (use_lockfile = true, or a DynamoDB table)"
                continue
            done = lz(tools, second, args, env, TIMEOUT_LOCKED)
            report = lz_env.parse_json_output(done.out)
            os.killpg(process.pid, signal.SIGCONT)
            stopped = False
            process.wait(timeout=TIMEOUT_PLAN)
            if done.code != 3 or not isinstance(report, dict) or report.get("error") != "state-locked":
                return f"a second `bin/lz plan dev` while the first holds the lock must fail at once with exit 3 and {{\"error\": \"state-locked\"}}; got exit {done.code}: {done.tail(4)}"
            if process.returncode != 0:
                return f"the first plan failed after the second was refused (exit {process.returncode}): {lz_env.Result(process.returncode, read_text(out_path), read_text(err_path)).tail(4)}"
            if lock_present(client):
                return "the lock is still there after both plans finished: lz must let tofu release it"
            return None
        finally:
            if process.poll() is None:
                if stopped:
                    os.killpg(process.pid, signal.SIGCONT)
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=30)
    return "could not observe the lock"


def drift_report(tools: Tools, cwd: str, env: Dict[str, str]) -> Tuple[Optional[Dict[str, Any]], lz_env.Result]:
    done = lz(tools, cwd, ["drift", "dev", "--json"], env, TIMEOUT_PLAN)
    report = lz_env.parse_json_output(done.out)
    return (report if isinstance(report, dict) else None), done


def drift_problems(tools: Tools, cwd: str, env: Dict[str, str], client: Any) -> Optional[str]:
    report, done = drift_report(tools, cwd, env)
    if report is None or done.code != 0 or report.get("drifted") or report.get("resources"):
        return f"right after `bin/lz apply dev`, `bin/lz drift dev --json` must exit 0 with drifted false and no resources; got exit {done.code}: {json.dumps(report)[:300] if report else done.tail(4)}"
    vpcs = [v for v in client.ec2_items("DescribeVpcs", "vpcSet") if lz_aws.tag_map(v.get("tagSet")).get("env") == "dev"]
    if len(vpcs) != 1:
        return f"expected one VPC tagged env=dev, found {len(vpcs)}"
    vpc_id = vpcs[0]["vpcId"]
    client.ec2("CreateTags", {"ResourceId.1": vpc_id, "Tag.1.Key": "owner", "Tag.1.Value": "changed-by-the-grader"})
    report, done = drift_report(tools, cwd, env)
    if report is None or done.code != 2:
        return f"after the grader changed tag owner on {vpc_id}, `bin/lz drift dev --json` must exit 2; got exit {done.code}: {done.tail(4)}"
    hits = [r for r in report.get("resources") or [] if isinstance(r, dict) and r.get("id") == vpc_id]
    if not hits:
        return f"drift report does not name {vpc_id} (resources: {[r.get('address') for r in report.get('resources') or [] if isinstance(r, dict)][:4]}); each entry needs address, type, id and attributes"
    attributes = [str(a) for r in hits for a in r.get("attributes") or []]
    if not any("tags" in a and "owner" in a for a in attributes):
        return f"drift on {hits[0].get('address')} must list the changed attribute tags.owner; got {attributes[:5]}"
    return None


def check_state_drift(project: str, tools: Optional[Tools] = None) -> Result:
    tools = tools or Tools()
    blocker = preflight(project, tools, ("dev",))
    if blocker:
        return False, blocker
    endpoint, error = start_moto(tools)
    if not endpoint:
        return False, error
    env = lz_env.aws_env(endpoint)
    client = tools.client(endpoint)
    with tools.workspace(project) as workspace:
        first = workspace.copy("operator-a")
        second = workspace.copy("operator-b")
        failed = apply_envs(tools, first, env, [["dev"]])
        if failed:
            return False, failed
        states = [f"{b}/{k}" for b in client.s3_buckets() for k in client.s3_keys(b) if k.endswith(".tfstate")]
        if not states:
            return False, "no *.tfstate object in any S3 bucket on moto after `bin/lz apply dev`: configure backend \"s3\" and point it at LZ_AWS_ENDPOINT"
        problem = lock_problem(tools, first, second, env, client) or drift_problems(tools, first, env, client)
    if problem:
        return False, problem
    return True, f"state in s3://{states[0]}; a second plan was refused by the lock; drift on tags.owner found, a clean env reported none"
