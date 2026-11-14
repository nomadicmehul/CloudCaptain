import contextlib
import datetime
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from lzcli import aws, cli, drift, tofu  # noqa: E402

spec = importlib.util.spec_from_file_location("gate_under_test", os.path.join(ROOT, "policy", "gate.py"))
gate = importlib.util.module_from_spec(spec)
sys.modules["gate_under_test"] = gate
spec.loader.exec_module(gate)

TAGS = {"owner": "team", "env": "dev", "cost-center": "cc-1"}


def change(address, rtype, after, unknown=None):
    return {"address": address, "mode": "managed", "type": rtype, "name": address.split(".")[-1].split("[")[0], "change": {"actions": ["create"], "after": after, "after_unknown": unknown or {"id": True}}}


def plan(*changes, module_resources=None):
    return {"format_version": "1.2", "resource_changes": list(changes), "configuration": {"root_module": {"module_calls": {"baseline": {"module": {"resources": module_resources or []}}}}}}


def bucket_plan(public_access=True, encrypted=True):
    refs = {"bucket": {"references": ["aws_s3_bucket.logs.id", "aws_s3_bucket.logs"]}}
    changes = [change("module.baseline.aws_s3_bucket.logs", "aws_s3_bucket", {"bucket": "logs", "tags": TAGS, "tags_all": TAGS})]
    resources = []
    if public_access:
        changes.append(change("module.baseline.aws_s3_bucket_public_access_block.logs", "aws_s3_bucket_public_access_block", {"block_public_acls": True, "block_public_policy": True, "ignore_public_acls": True, "restrict_public_buckets": True}, {"bucket": True}))
        resources.append({"address": "aws_s3_bucket_public_access_block.logs", "expressions": refs})
    if encrypted:
        changes.append(change("module.baseline.aws_s3_bucket_server_side_encryption_configuration.logs", "aws_s3_bucket_server_side_encryption_configuration", {"rule": [{"apply_server_side_encryption_by_default": [{"sse_algorithm": "aws:kms"}]}]}, {"bucket": True}))
        resources.append({"address": "aws_s3_bucket_server_side_encryption_configuration.logs", "expressions": refs})
    return plan(*changes, module_resources=resources)


def rules(found):
    return [(v.rule, v.address) for v in found]


class FakeRunner:
    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def __call__(self, argv, cwd, env):
        self.calls.append((list(argv), cwd, dict(env)))
        verb = argv[1]
        if verb == "show":
            return tofu.Completed(0, json.dumps({"resource_changes": [change("aws_vpc.this", "aws_vpc", {"cidr_block": "10.1.0.0/16"})], "resource_drift": []}), "")
        return self.results.get(verb, tofu.Completed(0, "{}", ""))


class PolicyTests(unittest.TestCase):
    def test_private_encrypted_bucket_passes(self):
        self.assertEqual(gate.evaluate(bucket_plan()), [])

    def test_bucket_without_access_block_or_encryption_is_flagged_on_the_bucket(self):
        found = rules(gate.evaluate(bucket_plan(public_access=False, encrypted=False)))
        self.assertEqual(found, [("encryption", "module.baseline.aws_s3_bucket.logs"), ("no-public-s3", "module.baseline.aws_s3_bucket.logs")])

    def test_open_ingress_only_allowed_for_https_on_load_balancer_groups(self):
        def group(address, port, role):
            tags = dict(TAGS, role=role)
            return change(address, "aws_security_group", {"ingress": [{"cidr_blocks": ["0.0.0.0/0"], "from_port": port, "to_port": port, "protocol": "tcp"}], "tags": tags, "tags_all": tags})

        found = rules(gate.evaluate(plan(group("aws_security_group.lb", 443, "load-balancer"), group("aws_security_group.app", 443, "app"), group("aws_security_group.ssh", 22, "load-balancer"))))
        self.assertEqual(found, [("no-open-ingress", "aws_security_group.app"), ("no-open-ingress", "aws_security_group.ssh")])

    def test_required_tags_and_cidr_allocation(self):
        vpc = change("aws_vpc.this", "aws_vpc", {"cidr_block": "192.168.0.0/16", "tags": {"owner": "team"}, "tags_all": {"owner": "team", "env": ""}})
        subnet = change("aws_subnet.private[1]", "aws_subnet", {"cidr_block": "10.1.16.0/20", "tags": TAGS, "tags_all": TAGS})
        route = change("aws_route.hub", "aws_route", {"destination_cidr_block": "0.0.0.0/0"})
        self.assertEqual(rules(gate.evaluate(plan(vpc, subnet, route))), [("cidr-allocation", "aws_vpc.this"), ("required-tags", "aws_vpc.this")])

    def test_a_state_file_is_not_a_plan(self):
        with self.assertRaises(gate.PlanError):
            gate.evaluate({"values": {}})


class DriftTests(unittest.TestCase):
    def test_changed_paths_walk_maps_and_lists(self):
        before = {"tags": {"owner": "team", "env": "dev"}, "ingress": [{"from_port": 443}], "gone": None}
        after = {"tags": {"owner": "someone", "env": "dev"}, "ingress": [{"from_port": 22}]}
        self.assertEqual(drift.changed_paths(before, after), ["ingress[0].from_port", "tags.owner"])

    def test_report_lists_each_drifted_resource_with_its_id(self):
        planned = {"resource_drift": [
            {"address": "module.network.aws_vpc.this", "type": "aws_vpc", "change": {"actions": ["update"], "before": {"id": "vpc-1", "tags": {"owner": "a"}}, "after": {"id": "vpc-1", "tags": {"owner": "b"}}}},
            {"address": "aws_s3_bucket.logs", "type": "aws_s3_bucket", "change": {"actions": ["delete"], "before": {"id": "logs"}, "after": None}},
            {"address": "aws_iam_role.x", "type": "aws_iam_role", "change": {"actions": ["update"], "before": {"id": "x"}, "after": {"id": "x"}}},
        ]}
        report = drift.drift_report("dev", planned)
        self.assertTrue(report["drifted"])
        self.assertEqual([(r["address"], r["id"], r["attributes"]) for r in report["resources"]], [
            ("aws_s3_bucket.logs", "logs", ["(deleted outside OpenTofu)"]),
            ("module.network.aws_vpc.this", "vpc-1", ["tags.owner"]),
        ])


class TofuTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "envs", "dev"))
        open(os.path.join(self.root, "envs", "dev", "main.tf"), "w").close()

    def test_remote_endpoints_are_refused(self):
        with self.assertRaises(tofu.LzError) as caught:
            tofu.Tofu(self.root, {"LZ_AWS_ENDPOINT": "https://s3.eu-central-1.amazonaws.com"})
        self.assertEqual(caught.exception.code, 2)

    def test_lock_error_becomes_exit_3_with_the_holder(self):
        text = "Error: Error acquiring the state lock\n\nLock Info:\n  ID:        5f1c-77\n  Path:      lz-tfstate/envs/dev/terraform.tfstate\n  Operation: OperationTypePlan\n  Who:       ci@runner\n"
        runner = FakeRunner({"plan": tofu.Completed(1, "", text)})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["plan", "dev", "--json"], runner=runner, root=self.root, environ={})
        self.assertEqual(code, 3)
        report = json.loads(out.getvalue())
        self.assertEqual((report["error"], report["lock"]["id"], report["lock"]["who"]), ("state-locked", "5f1c-77", "ci@runner"))

    def test_plan_writes_the_plan_json_and_passes_the_endpoint(self):
        runner = FakeRunner()
        original = aws.ensure_bucket
        aws.ensure_bucket = lambda endpoint, bucket, region: "exists"
        self.addCleanup(setattr, aws, "ensure_bucket", original)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["plan", "dev", "--json"], runner=runner, root=self.root, environ={"LZ_AWS_ENDPOINT": "http://127.0.0.1:5000"})
        self.assertEqual(code, 0)
        summary = json.loads(out.getvalue())
        with open(summary["plan_json"], encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["resource_changes"][0]["type"], "aws_vpc")
        init = runner.calls[0]
        self.assertTrue(any(arg.startswith("-backend-config=") for arg in init[0]))
        self.assertEqual(init[2]["TF_VAR_aws_endpoint"], "http://127.0.0.1:5000")
        with open(os.path.join(self.root, ".lz", "backend-dev.hcl"), encoding="utf-8") as handle:
            self.assertIn('s3 = "http://127.0.0.1:5000"', handle.read())

    def test_unknown_env_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(["apply", "staging"], runner=FakeRunner(), root=self.root, environ={})
        self.assertEqual(code, 2)
        self.assertIn("known: dev", err.getvalue())

    def test_signature_header_is_well_formed(self):
        headers = aws.authorization("PUT", "http://127.0.0.1:5000/lz-tfstate", b"", "eu-central-1", "s3", datetime.datetime(2026, 11, 13, 9, 0, 0))
        self.assertEqual(headers["X-Amz-Date"], "20261113T090000Z")
        self.assertTrue(headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=testing/20261113/eu-central-1/s3/aws4_request, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature="))


if __name__ == "__main__":
    unittest.main()
