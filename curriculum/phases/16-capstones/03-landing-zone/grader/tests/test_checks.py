"""Tests for the landing-zone checks: fake runners, fake AWS clients, temp dirs. No Docker, no tofu, no network."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import checks  # noqa: E402
import lz_aws  # noqa: E402
import lz_env  # noqa: E402
import lz_hcl  # noqa: E402
import lz_iam  # noqa: E402

GOOD_VARIABLE = '''
variable "cidr_block" {
  type        = string
  description = "VPC range"

  validation {
    condition     = can(cidrhost(var.cidr_block, 0)) && "${var.cidr_block}" != "{"
    error_message = <<-EOT
      cidr_block must be a CIDR }
    EOT
  }
}
'''
GOOD_OUTPUT = 'output "network_id" {\n  description = "VPC id"\n  value       = { for k, v in local.x : k => v }\n}\n'


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class Runner:
    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or []

    def __call__(self, argv, cwd=None, env=None, timeout=60):
        self.calls.append(list(argv))
        for match, result in self.answers:
            if match(list(argv)):
                return result(list(argv)) if callable(result) else result
        return lz_env.Result(0, "", "")


class FakeMoto:
    def start(self):
        return "http://127.0.0.1:5999"

    def reset(self):
        pass


class Workspace:
    def __init__(self, project):
        self.root = tempfile.mkdtemp()
        self.project = project

    def copy(self, name="project"):
        target = os.path.join(self.root, name)
        shutil.copytree(self.project, target)
        return target

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.root, ignore_errors=True)


def tools(runner=None, client=None, which=lambda name: "/usr/bin/" + name):
    return checks.Tools(runner=runner or Runner(), moto=FakeMoto, client=lambda endpoint: client, which=which, workspace=Workspace)


def tags(**pairs):
    return [{"key": k.replace("_", "-"), "value": v} for k, v in pairs.items()]


class FakeEc2:
    def __init__(self):
        self.vpcs = {
            "hub": ("vpc-hub", "10.0.0.0/16"),
            "dev": ("vpc-dev", "10.1.0.0/16"),
            "prod": ("vpc-prod", "10.2.0.0/16"),
        }
        self.extra_rules = {}
        self.flow_logs = {"vpc-hub", "vpc-dev", "vpc-prod"}
        self.created_tags = []

    def ec2_items(self, action, key, filters=None):
        filters = filters or {}
        vpc = filters.get("vpc-id")
        if action == "DescribeVpcs":
            return [{"vpcId": vid, "cidrBlock": cidr, "tagSet": tags(env=env, owner="team", cost_center="cc")} for env, (vid, cidr) in self.vpcs.items()]
        if action == "DescribeSubnets":
            return [
                {"subnetId": f"{vpc}-a", "availabilityZone": "eu-central-1a", "mapPublicIpOnLaunch": "false"},
                {"subnetId": f"{vpc}-b", "availabilityZone": "eu-central-1b", "mapPublicIpOnLaunch": "false"},
                {"subnetId": f"{vpc}-pub", "availabilityZone": "eu-central-1a", "mapPublicIpOnLaunch": "false"},
            ]
        if action == "DescribeRouteTables":
            env = next(e for e, (vid, _) in self.vpcs.items() if vid == vpc)
            peer_routes = []
            if env == "hub":
                peer_routes = [{"destinationCidrBlock": "10.1.0.0/16", "vpcPeeringConnectionId": "pcx-dev"}, {"destinationCidrBlock": "10.2.0.0/16", "vpcPeeringConnectionId": "pcx-prod"}]
            else:
                peer_routes = [{"destinationCidrBlock": "10.0.0.0/16", "vpcPeeringConnectionId": f"pcx-{env}"}]
            return [
                {"routeTableId": f"rtb-{env}-main", "associationSet": [{"main": "true"}], "routeSet": [{"destinationCidrBlock": self.vpcs[env][1], "gatewayId": "local"}] + peer_routes},
                {"routeTableId": f"rtb-{env}-pub", "associationSet": [{"main": "false", "subnetId": f"{vpc}-pub"}], "routeSet": [{"destinationCidrBlock": "0.0.0.0/0", "gatewayId": "igw-1"}]},
            ]
        if action == "DescribeSecurityGroups":
            groups = [{
                "groupId": f"sg-{vpc}-lb", "groupName": "lb", "tagSet": tags(role="load-balancer"),
                "ipPermissions": [{"ipProtocol": "tcp", "fromPort": "443", "toPort": "443", "ipRanges": [{"cidrIp": "0.0.0.0/0"}]}],
            }]
            if vpc in self.extra_rules:
                groups.append({"groupId": f"sg-{vpc}-app", "groupName": "app", "tagSet": None, "ipPermissions": [self.extra_rules[vpc]]})
            return groups
        if action == "DescribeFlowLogs":
            return [{"flowLogId": f"fl-{vpc_id}", "resourceId": vpc_id} for vpc_id in sorted(self.flow_logs)]
        if action == "DescribeVpcPeeringConnections":
            return [
                {"vpcPeeringConnectionId": "pcx-dev", "requesterVpcInfo": {"vpcId": "vpc-hub"}, "accepterVpcInfo": {"vpcId": "vpc-dev"}, "status": {"code": "active"}},
                {"vpcPeeringConnectionId": "pcx-prod", "requesterVpcInfo": {"vpcId": "vpc-hub"}, "accepterVpcInfo": {"vpcId": "vpc-prod"}, "status": {"code": "active"}},
            ]
        raise AssertionError(action)

    def ec2(self, action, params):
        self.created_tags.append(params)
        return {}


def doc(*statements):
    return urllib.parse.quote(json.dumps({"Version": "2012-10-17", "Statement": list(statements)}))


GUARDRAIL = doc(
    {"Sid": "Region", "Effect": "Deny", "NotAction": ["iam:*", "sts:*", "organizations:*", "account:*"], "Resource": "*", "Condition": {"StringNotEquals": {"aws:RequestedRegion": ["eu-central-1"]}}},
    {"Sid": "Account", "Effect": "Deny", "Action": ["account:*", "organizations:*", "iam:CreateUser", "iam:DeleteRolePermissionsBoundary"], "Resource": "*"},
)
BOUNDARY = doc({"Effect": "Allow", "Action": ["ec2:*", "s3:*", "iam:Get*", "iam:List*"], "Resource": "*"})
DEPLOY = doc({"Effect": "Allow", "Action": ["ec2:*", "s3:*"], "Resource": "*"})
READONLY = doc({"Effect": "Allow", "Action": ["ec2:Describe*", "iam:Get*", "s3:GetBucket*"], "Resource": "*"})
ADMIN = doc({"Effect": "Allow", "Action": "*", "Resource": "*"})


class FakeIam:
    def __init__(self, deploy_policy=DEPLOY, boundary=True):
        self.policies = {"arn:guardrail": GUARDRAIL, "arn:boundary": BOUNDARY, "arn:deploy": deploy_policy, "arn:readonly": READONLY}
        self.boundary = boundary

    def iam(self, action, params=None):
        params = params or {}
        if action == "ListRoles":
            return {"Roles": [{"RoleName": "lz-dev-deploy"}, {"RoleName": "lz-dev-readonly"}], "IsTruncated": "false"}
        if action == "GetRole":
            role = {"RoleName": params["RoleName"], "Tags": [{"Key": "env", "Value": "dev"}]}
            if self.boundary:
                role["PermissionsBoundary"] = {"PermissionsBoundaryArn": "arn:boundary"}
            return {"Role": role}
        if action == "ListAttachedRolePolicies":
            access = "arn:deploy" if params["RoleName"].endswith("deploy") else "arn:readonly"
            return {"AttachedPolicies": [{"PolicyName": access, "PolicyArn": access}, {"PolicyName": "guardrail", "PolicyArn": "arn:guardrail"}]}
        if action == "ListRolePolicies":
            return {"PolicyNames": None}
        if action == "GetPolicy":
            return {"Policy": {"DefaultVersionId": "v1"}}
        if action == "GetPolicyVersion":
            return {"PolicyVersion": {"Document": self.policies[params["PolicyArn"]]}}
        raise AssertionError(action)


class HclTests(unittest.TestCase):
    def test_scanner_skips_strings_heredocs_and_comments(self):
        text = "# variable \"fake\" {\n/* output \"no\" { } */\n" + GOOD_VARIABLE + GOOD_OUTPUT
        root = lz_hcl.parse(text)
        variable = lz_hcl.top_blocks({"x": root}, "variable")[0]
        self.assertEqual(variable.labels, ["cidr_block"])
        self.assertEqual(variable.attributes, ["type", "description"])
        self.assertEqual(len(variable.children("validation")), 1)
        self.assertEqual([b.labels for b in root.children("output")], [["network_id"]])

    def test_one_line_blocks_and_unclosed_braces(self):
        root = lz_hcl.parse('variable "x" { type = string }\nvariable "y" { default = {} }\n')
        self.assertEqual([b.attributes for b in root.blocks], [["type"], ["default"]])
        with self.assertRaises(lz_hcl.HclError):
            lz_hcl.parse('variable "x" {\n  type = string\n')


class ModuleStructureTests(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.project, True)

    def make_modules(self, variable=GOOD_VARIABLE):
        for name in checks.AWS_MODULES:
            write(os.path.join(self.project, "modules", name, "variables.tf"), variable)
            write(os.path.join(self.project, "modules", name, "outputs.tf"), GOOD_OUTPUT)
        for env in checks.ENVS:
            write(os.path.join(self.project, "envs", env, "main.tf"), 'module "network" {\n  source = "../../modules/network"\n}\n')

    def test_missing_modules_fail_fast_with_their_names(self):
        passed, detail = checks.check_module_structure(self.project, tools())
        self.assertFalse(passed)
        self.assertIn("modules/network, modules/identity, modules/baseline missing", detail)

    def test_untyped_variable_and_missing_validation_are_named(self):
        self.make_modules('variable "zones" {\n  description = "AZs"\n}\n')
        passed, detail = checks.check_module_structure(self.project, tools())
        self.assertFalse(passed)
        self.assertIn('variable "zones" has no type', detail)
        self.assertIn("no variable has a validation block", detail)

    def test_fmt_and_validate_results_decide(self):
        self.make_modules()
        valid = lz_env.Result(0, json.dumps({"valid": True, "diagnostics": []}), "")
        runner = Runner([(lambda a: a[:2] == ["tofu", "validate"], valid)])
        passed, detail = checks.check_module_structure(self.project, tools(runner))
        self.assertTrue(passed, detail)
        self.assertEqual(sum(1 for c in runner.calls if c[:2] == ["tofu", "validate"]), 6)
        unformatted = lz_env.Result(3, "modules/network/main.tf\n", "")
        runner = Runner([(lambda a: a[:2] == ["tofu", "fmt"] and a[-1] == "modules", unformatted)])
        passed, detail = checks.check_module_structure(self.project, tools(runner))
        self.assertFalse(passed)
        self.assertIn("modules/network/main.tf", detail)

    def test_validate_error_points_at_file_and_line(self):
        self.make_modules()
        bad = {"valid": False, "diagnostics": [{"severity": "error", "summary": "Unsupported argument", "detail": "foo", "range": {"filename": "main.tf", "start": {"line": 7}}}]}
        runner = Runner([(lambda a: a[:2] == ["tofu", "validate"], lz_env.Result(1, json.dumps(bad), ""))])
        passed, detail = checks.check_module_structure(self.project, tools(runner))
        self.assertFalse(passed)
        self.assertIn("Unsupported argument", detail)
        self.assertIn("main.tf:7", detail)


class NetworkTests(unittest.TestCase):
    def test_a_correct_hub_and_spoke_has_no_problems(self):
        problems, summary = checks.network_problems(FakeEc2())
        self.assertEqual(problems, [])
        self.assertIn("dev 10.1.0.0/16 (2 private subnets)", summary)

    def test_overlap_open_ssh_missing_flow_log_are_reported(self):
        ec2 = FakeEc2()
        ec2.vpcs["prod"] = ("vpc-prod", "10.1.128.0/17")
        ec2.extra_rules["vpc-dev"] = {"ipProtocol": "tcp", "fromPort": "22", "toPort": "22", "ipRanges": [{"cidrIp": "0.0.0.0/0"}]}
        ec2.flow_logs.discard("vpc-hub")
        problems, _ = checks.network_problems(ec2)
        text = " ".join(problems)
        self.assertIn("dev 10.1.0.0/16 overlaps prod 10.1.128.0/17", text)
        self.assertIn("allows tcp 22-22 from the internet", text)
        self.assertIn("hub: no flow log", text)

    def test_subnet_routed_to_an_internet_gateway_is_not_private(self):
        ec2 = FakeEc2()
        subnets = ec2.ec2_items("DescribeSubnets", "subnetSet", {"vpc-id": "vpc-dev"})
        tables = ec2.ec2_items("DescribeRouteTables", "routeTableSet", {"vpc-id": "vpc-dev"})
        self.assertEqual([s["subnetId"] for s in checks.private_subnets(subnets, tables)], ["vpc-dev-a", "vpc-dev-b"])

    def test_network_check_applies_spokes_before_the_hub(self):
        project = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, project, True)
        write(os.path.join(project, "bin", "lz"), "#!/bin/sh\n")
        os.chmod(os.path.join(project, "bin", "lz"), 0o755)
        for env in checks.ENVS:
            write(os.path.join(project, "envs", env, "main.tf"), "")
        order = []
        runner = Runner([(lambda a: a[1] == "apply", lambda a: order.append(a[2]) or lz_env.Result(0, "{}", ""))])
        passed, detail = checks.check_network_baseline(project, tools(runner, FakeEc2()))
        self.assertTrue(passed, detail)
        self.assertEqual(sorted(order[:2]), ["dev", "prod"])
        self.assertEqual(order[2], "hub")


class IdentityTests(unittest.TestCase):
    def test_evaluator_explicit_deny_boundary_and_allow(self):
        role = lz_iam.RolePolicies("r", [("deploy", lz_iam.decode_document(DEPLOY)), ("guardrail", lz_iam.decode_document(GUARDRAIL))], ("boundary", lz_iam.decode_document(READONLY)))
        self.assertEqual(lz_iam.evaluate(role, "ec2:CreateVpc", "*", {"aws:RequestedRegion": "us-east-1"}).decision, "explicitDeny")
        self.assertEqual(lz_iam.evaluate(role, "ec2:CreateVpc", "*", {"aws:RequestedRegion": "eu-central-1"}).decision, "implicitDeny")
        self.assertTrue(lz_iam.evaluate(role, "ec2:DescribeVpcs", "*", {"aws:RequestedRegion": "eu-central-1"}).allowed)

    def test_guarded_roles_pass(self):
        problems, summary = checks.identity_problems(FakeIam())
        self.assertEqual(problems, [])
        self.assertIn("2 roles with boundaries", summary)

    def test_admin_policy_and_missing_boundary_fail(self):
        problems, _ = checks.identity_problems(FakeIam(deploy_policy=ADMIN, boundary=False))
        text = " ".join(problems)
        self.assertIn("lz-dev-deploy has no permissions boundary", text)
        self.assertIn('allows Action "*" on Resource "*"', text)


class PolicyGateTests(unittest.TestCase):
    def test_every_fixture_expectation_names_a_resource_in_its_plan(self):
        cases = checks.load_policy_cases()
        self.assertGreaterEqual(len(cases), 10)
        self.assertEqual(len([c for c in cases if not c["expect"]]), 1)
        rules = set()
        for case in cases:
            with open(os.path.join(checks.FIXTURES, "plans", case["file"]), encoding="utf-8") as handle:
                plan = json.load(handle)
            addresses = {c["address"] for c in plan["resource_changes"]}
            for expected in case["expect"]:
                rules.add(expected["rule"])
                self.assertIn(expected["address"], addresses, case["file"])
        self.assertEqual(rules, {"required-tags", "no-public-s3", "no-open-ingress", "encryption", "cidr-allocation"})

    def test_a_stub_policy_command_fails_fast(self):
        project = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, project, True)
        write(os.path.join(project, "bin", "lz"), "#!/bin/sh\n")
        os.chmod(os.path.join(project, "bin", "lz"), 0o755)
        runner = Runner([(lambda a: a[1] == "policy", lz_env.Result(1, "", "not implemented: milestone policy-gate"))])
        passed, detail = checks.check_policy_gate(project, tools(runner))
        self.assertFalse(passed)
        self.assertIn("not implemented", detail)
        self.assertEqual(len(runner.calls), 1)

    def test_a_policy_that_finds_the_expected_violations_reaches_the_own_plans(self):
        project = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, project, True)
        write(os.path.join(project, "bin", "lz"), "#!/bin/sh\n")
        os.chmod(os.path.join(project, "bin", "lz"), 0o755)
        expectations = {c["file"]: c["expect"] for c in checks.load_policy_cases()}
        plan_path = os.path.join(project, "plan.json")
        write(plan_path, json.dumps({"resource_changes": [{"type": "aws_vpc", "address": "module.network.aws_vpc.this"}]}))

        def policy(argv):
            name = os.path.basename(argv[2])
            found = expectations.get(name, [])
            return lz_env.Result(1 if found else 0, json.dumps({"violations": [dict(e, message="m") for e in found]}), "")

        runner = Runner([
            (lambda a: a[1] == "policy", policy),
            (lambda a: a[1] == "plan", lz_env.Result(0, "Initializing...\n" + json.dumps({"env": "dev", "plan_json": plan_path}), "")),
        ])
        passed, detail = checks.check_policy_gate(project, tools(runner))
        self.assertFalse(passed)
        self.assertIn("envs/hub, envs/dev, envs/prod missing", detail)
        for env in checks.ENVS:
            write(os.path.join(project, "envs", env, "main.tf"), "")
        passed, detail = checks.check_policy_gate(project, tools(runner))
        self.assertTrue(passed, detail)


class MultiCloudTests(unittest.TestCase):
    def test_interface_test_is_rendered_per_provider(self):
        text = checks.render_interface_test("azurerm")
        self.assertIn("mock_resource \"azurerm_subnet\"", text)
        self.assertIn('zones = ["1", "2", "3"]', text)
        self.assertNotIn("@", text)
        self.assertEqual(len(lz_hcl.parse(text).children("run")), 4)

    def test_missing_outputs_and_too_few_runs_are_reported(self):
        project = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, project, True)
        for module in checks.CLOUD_MODULES:
            variables = "".join(f'variable "{v}" {{\n  type = string\n}}\n' for v in checks.NETWORK_INPUTS)
            outputs = "".join(f'output "{o}" {{\n  value = 1\n}}\n' for o in checks.NETWORK_OUTPUTS if module != "network-gcp" or o != "public_ingress_ports")
            write(os.path.join(project, "modules", module, "main.tf"), variables + outputs)
        write(os.path.join(project, "modules", "network-gcp", "tests", "a.tftest.hcl"), 'mock_provider "google" {}\nrun "one" {\n  command = plan\n}\n')
        write(os.path.join(project, "modules", "network-azure", "tests", "a.tftest.hcl"), 'run "a" {}\nrun "b" {}\nrun "c" {}\n')
        passed, detail = checks.check_multi_cloud(project, tools())
        self.assertFalse(passed)
        self.assertIn("modules/network-gcp lacks output public_ingress_ports", detail)
        self.assertIn("has 1 run block(s)", detail)
        self.assertIn('never declares mock_provider "azurerm"', detail)


class StateDriftTests(unittest.TestCase):
    def test_lock_is_seen_in_s3_and_dynamodb(self):
        class Client:
            def __init__(self, keys, items):
                self.keys, self.items = keys, items

            def s3_buckets(self):
                return ["lz-tfstate"]

            def s3_keys(self, bucket):
                return self.keys

            def dynamodb(self, operation, payload):
                return {"TableNames": ["locks"]} if operation == "ListTables" else {"Items": self.items}

        self.assertTrue(checks.lock_present(Client(["envs/dev/terraform.tfstate", "envs/dev/terraform.tfstate.tflock"], [])))
        self.assertFalse(checks.lock_present(Client(["envs/dev/terraform.tfstate"], [{"LockID": {"S": "b/k-md5"}, "Digest": {"S": "x"}}])))
        self.assertTrue(checks.lock_present(Client([], [{"LockID": {"S": "b/k"}, "Info": {"S": "{}"}}])))

    def test_drift_must_name_the_tampered_vpc_and_attribute(self):
        ec2 = FakeEc2()
        reports = [
            (0, {"env": "dev", "drifted": False, "resources": []}),
            (2, {"env": "dev", "drifted": True, "resources": [{"address": "module.network.aws_vpc.this", "type": "aws_vpc", "id": "vpc-dev", "attributes": ["tags.owner", "tags_all.owner"]}]}),
        ]
        runner = Runner([(lambda a: a[1] == "drift", lambda a: lz_env.Result(reports[0][0], json.dumps(reports.pop(0)[1]), ""))])
        self.assertIsNone(checks.drift_problems(tools(runner), "/tmp", {}, ec2))
        self.assertEqual(ec2.created_tags[0]["ResourceId.1"], "vpc-dev")
        runner = Runner([(lambda a: a[1] == "drift", lz_env.Result(2, json.dumps({"drifted": True, "resources": []}), ""))])
        self.assertIn("must exit 0", checks.drift_problems(tools(runner), "/tmp", {}, FakeEc2()))


class SafetyTests(unittest.TestCase):
    def test_only_local_endpoints_are_accepted(self):
        self.assertEqual(lz_aws.require_local("http://localhost:5000/"), "http://localhost:5000")
        for bad in ("https://ec2.eu-central-1.amazonaws.com", "http://10.0.0.5:5000", "localhost:5000"):
            with self.assertRaises(lz_aws.EndpointError):
                lz_aws.require_local(bad)
        moto = lz_env.Moto(runner=Runner(), which=lambda name: None, environ={"LZ_AWS_ENDPOINT": "https://s3.amazonaws.com"})
        with self.assertRaisesRegex(lz_env.SetupError, "not local"):
            moto.start()
        with self.assertRaisesRegex(lz_env.SetupError, "Docker is not installed"):
            lz_env.Moto(runner=Runner(), which=lambda name: None, environ={}).start()

    def test_signing_key_matches_the_aws_documentation_example(self):
        key = lz_aws.signing_key("wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20120215", "us-east-1", "iam")
        self.assertEqual(key.hex(), "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d")
        headers = lz_aws.sign("POST", "http://127.0.0.1:5000/", {"Content-Type": "application/x-www-form-urlencoded"}, b"Action=DescribeVpcs", "ec2")
        self.assertRegex(headers["Authorization"], r"^AWS4-HMAC-SHA256 Credential=testing/\d{8}/eu-central-1/ec2/aws4_request, SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date, Signature=[0-9a-f]{64}$")

    def test_xml_lists_and_noisy_json_output_are_parsed(self):
        import xml.etree.ElementTree as ET
        xml = '<R xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"><vpcSet><item><vpcId>vpc-1</vpcId><tagSet><item><key>env</key><value>dev</value></item></tagSet></item></vpcSet></R>'
        value = lz_aws.xml_value(ET.fromstring(xml))
        self.assertEqual(lz_aws.tag_map(value["vpcSet"][0]["tagSet"]), {"env": "dev"})
        self.assertEqual(lz_env.parse_json_output('Initializing the backend...\n{\n  "plan_json": "p.json"\n}\n'), {"plan_json": "p.json"})
        self.assertIsNone(lz_env.parse_json_output("not implemented"))

    def test_every_check_fails_fast_on_an_empty_project(self):
        project = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, project, True)
        runner = Runner()
        started = time.monotonic()
        for milestone in ("module_structure", "network_baseline", "identity_guardrails", "policy_gate", "multi_cloud", "state_drift"):
            passed, detail = getattr(checks, "check_" + milestone)(project, tools(runner))
            self.assertFalse(passed)
            self.assertTrue(detail)
        self.assertEqual(runner.calls, [])
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
