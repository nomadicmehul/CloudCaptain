import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
ACCOUNT = os.path.join(FIXTURES, "account.json")
POLICIES = os.path.join(FIXTURES, "policies")
A = "123456789012"
ALICE = f"arn:aws:iam::{A}:user/alice"
BOB = f"arn:aws:iam::{A}:user/bob"
CAROL = f"arn:aws:iam::{A}:user/carol"
ERIN = f"arn:aws:iam::{A}:user/erin"
READER = f"arn:aws:sts::{A}:assumed-role/reader/s1"
AUDITOR = f"arn:aws:sts::{A}:assumed-role/auditor/s1"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def policy_file(name):
    with open(os.path.join(POLICIES, name), encoding="utf-8") as handle:
        return json.load(handle)


def pol(*statements, version="2012-10-17"):
    return {"Version": version, "Statement": list(statements)}


def stmt(effect, action, resource="*", **extra):
    return dict({"Effect": effect, "Action": action, "Resource": resource}, **extra)


def scenario(principal, identity=(), **kwargs):
    return main.Scenario(main.parse_principal(principal), [main.make_policy("identity", f"p{n}", d) for n, d in enumerate(identity)], **kwargs)


def decide(sc, action, resource="*", **context):
    return main.evaluate(sc, main.Request(action, resource, context), NOW)


def from_account(principal, action, resource="*", resource_policy=None, session=None, **context):
    directory = main.load_directory(fixture("account.json"))
    p = main.parse_principal(principal)
    identity, boundary, warnings = main.directory_policies(directory, p)
    sc = main.Scenario(p, identity, boundary, session=session, resource_policy=resource_policy)
    request = main.Request(action, resource, context)
    return main.evaluate(main.with_trust(sc, directory, request), request, NOW), warnings


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class MatchingTests(unittest.TestCase):
    def test_actions_match_case_insensitively_with_both_wildcards(self):
        self.assertTrue(main.action_matches("s3:Get*", "s3:getobject"))
        self.assertTrue(main.action_matches("s3:*Object*", "s3:PutObjectAcl"))
        self.assertTrue(main.action_matches("ec2:Describe?nstances", "ec2:DescribeInstances"))
        self.assertTrue(main.action_matches("*", "iam:CreateUser"))
        self.assertFalse(main.action_matches("s3:Get*", "s3:PutObject"))
        self.assertFalse(main.action_matches("s3:GetObject", "s3:GetObjectAcl"))

    def test_resource_wildcards_follow_the_resource_element_documentation(self):
        pattern = "arn:aws:s3:::amzn-s3-demo-bucket/*/test/*"
        for key in ["1/test/object.jpg", "1/2/test/object.jpg", "1/2/test/3/object.jpg", "1/2/3/test/4/object.jpg", "1///test///object.jpg", "1/test/.jpg", "/test/object.jpg", "1/test/"]:
            self.assertTrue(main.resource_matches(pattern, "arn:aws:s3:::amzn-s3-demo-bucket/" + key, main.EMPTY, True), key)
        for key in ["1-test/object.jpg", "test/object.jpg", "1/2/test.jpg"]:
            self.assertFalse(main.resource_matches(pattern, "arn:aws:s3:::amzn-s3-demo-bucket/" + key, main.EMPTY, True), key)

    def test_resource_matching_is_case_sensitive_and_segment_aware(self):
        self.assertFalse(main.resource_matches("arn:aws:iam::123456789012:user/Bob", "arn:aws:iam::123456789012:user/bob", main.EMPTY, True))
        self.assertTrue(main.resource_matches("arn:aws:ec2:*:*:instance/*", "arn:aws:ec2:eu-central-1:123456789012:instance/i-0abc", main.EMPTY, True))
        self.assertFalse(main.resource_matches("arn:aws:s3:::cc-*-data", "arn:aws:s3:::cc-a:b-data", main.EMPTY, True))
        self.assertTrue(main.resource_matches("arn:aws:s3:*", "arn:aws:s3:::cc-data/key", main.EMPTY, True))

    def test_policy_variables_defaults_and_special_characters(self):
        ctx = main.Context({"aws:username": "carol"})
        self.assertTrue(main.glob_matches("home/${aws:username}/*", "home/carol/notes.txt", ctx, True))
        self.assertFalse(main.glob_matches("home/${aws:username}/*", "home/bob/notes.txt", ctx, True))
        self.assertTrue(main.glob_matches("bucket-${aws:PrincipalTag/team, 'company-wide'}", "bucket-company-wide", ctx, True))
        self.assertTrue(main.glob_matches("price${$}${*}", "price$*", ctx, True))
        self.assertFalse(main.glob_matches("price${*}", "price-anything", ctx, True))
        self.assertIsNone(main.compile_glob("home/${aws:userid}/*", ctx, True))
        self.assertTrue(main.glob_matches("home/${aws:username}", "home/${aws:username}", ctx, False))


class ConditionTests(unittest.TestCase):
    def test_every_documented_condition_example(self):
        rows = fixture("aws-doc-conditions.json")["rows"]
        self.assertGreaterEqual(len(rows), 40)
        for row in rows:
            holds, _, _ = main.condition_result(row["condition"], main.Context(row["context"]), True)
            self.assertEqual(holds, row["expect"], row)

    def test_negated_operators_are_nor_and_true_when_the_key_is_missing(self):
        block = {"StringNotEquals": {"aws:RequestedRegion": ["eu-central-1", "eu-west-1"]}}
        self.assertTrue(main.condition_result(block, main.Context({"aws:RequestedRegion": "us-east-1"}), True)[0])
        self.assertFalse(main.condition_result(block, main.Context({"aws:RequestedRegion": "eu-west-1"}), True)[0])
        holds, missing, _ = main.condition_result(block, main.Context(), True)
        self.assertTrue(holds)
        self.assertEqual(missing, ["aws:RequestedRegion"])

    def test_numeric_date_ip_and_null_operators(self):
        ctx = main.Context({"s3:max-keys": "10", "aws:CurrentTime": "2026-11-01T12:00:00Z", "aws:SourceIp": "2001:db8::5"})
        self.assertTrue(main.condition_result({"NumericLessThanEquals": {"s3:max-keys": "10"}}, ctx, True)[0])
        self.assertFalse(main.condition_result({"NumericLessThan": {"s3:max-keys": "10"}}, ctx, True)[0])
        self.assertTrue(main.condition_result({"DateLessThan": {"aws:CurrentTime": "2026-12-31T00:00:00Z"}}, ctx, True)[0])
        self.assertFalse(main.condition_result({"DateGreaterThan": {"aws:CurrentTime": "2026-12-31"}}, ctx, True)[0])
        self.assertTrue(main.condition_result({"IpAddress": {"aws:SourceIp": ["203.0.113.0/24", "2001:db8::/32"]}}, ctx, True)[0])
        self.assertTrue(main.condition_result({"NotIpAddress": {"aws:SourceIp": "203.0.113.0/24"}}, ctx, True)[0])
        self.assertTrue(main.condition_result({"Null": {"aws:TokenIssueTime": "true"}}, ctx, True)[0])
        self.assertFalse(main.condition_result({"Null": {"aws:SourceIp": "true"}}, ctx, True)[0])

    def test_condition_key_names_are_case_insensitive_values_are_not(self):
        ctx = main.Context({"AWS:PRINCIPALTAG/Team": "data"})
        self.assertTrue(main.condition_result({"StringEquals": {"aws:PrincipalTag/team": "data"}}, ctx, True)[0])
        self.assertFalse(main.condition_result({"StringEquals": {"aws:PrincipalTag/team": "Data"}}, ctx, True)[0])

    def test_unknown_operators_are_errors(self):
        with self.assertRaises(main.PolicyError):
            main.condition_result({"StringEqualz": {"k": "v"}}, main.Context(), True)
        with self.assertRaises(main.PolicyError):
            main.condition_result({"ForEveryValue:StringEquals": {"k": "v"}}, main.Context(), True)
        with self.assertRaises(main.PolicyError):
            main.parse_operator("NullIfExists")


class PrincipalTests(unittest.TestCase):
    def test_parse_principal_kinds(self):
        self.assertEqual(main.parse_principal(ALICE).kind, "User")
        session = main.parse_principal(READER)
        self.assertEqual((session.kind, session.name, session.principal_arn), ("AssumedRole", "reader", f"arn:aws:iam::{A}:role/reader"))
        self.assertEqual(main.parse_principal(A).kind, "Account")
        self.assertEqual(main.parse_principal("anonymous").kind, "Anonymous")
        self.assertEqual(main.parse_principal(f"arn:aws:sts::{A}:federated-user/ops").kind, "FederatedUser")
        with self.assertRaises(main.PolicyError):
            main.parse_principal("alice")

    def test_principal_element_grants_rank_direct_over_role_over_account(self):
        session = main.parse_principal(READER)
        self.assertEqual(main.principal_grant({"AWS": A}, session), "account")
        self.assertEqual(main.principal_grant({"AWS": [A, f"arn:aws:iam::{A}:role/team/reader"]}, session), "role")
        self.assertEqual(main.principal_grant({"AWS": READER}, session), "session")
        self.assertEqual(main.principal_grant("*", session), "wildcard")
        self.assertEqual(main.principal_grant({"AWS": "AROAEXAMPLEREADER001"}, session), "")
        self.assertEqual(main.principal_grant({"AWS": "arn:aws:iam::444455556666:root"}, session), "")
        self.assertEqual(main.principal_grant({"Service": "lambda.amazonaws.com"}, session), "")


class EvaluateTests(unittest.TestCase):
    def test_explicit_deny_wins_over_any_allow(self):
        sc = scenario(ALICE, [pol(stmt("Allow", "s3:*")), pol(stmt("Deny", "s3:DeleteObject", Sid="NoDelete"))])
        decision = decide(sc, "s3:DeleteObject", "arn:aws:s3:::cc-data/a")
        self.assertEqual((decision.decision, decision.kind, decision.match.sid), ("explicitDeny", "identity", "NoDelete"))
        self.assertTrue(decide(sc, "s3:PutObject", "arn:aws:s3:::cc-data/a").allowed)

    def test_nothing_allows_is_an_implicit_deny_with_the_aws_message(self):
        decision = decide(scenario(ALICE), "iam:ListUsers")
        self.assertEqual(decision.decision, "implicitDeny")
        self.assertEqual(decision.message, f"User: {ALICE} is not authorized to perform: iam:ListUsers because no identity-based policy allows the iam:ListUsers action")

    def test_not_action_and_not_resource(self):
        sc = scenario(BOB, [policy_file("bob-poweruser.json")])
        self.assertTrue(decide(sc, "sqs:SendMessage", f"arn:aws:sqs:us-east-1:{A}:cc-jobs").allowed)
        self.assertFalse(decide(sc, "iam:CreateUser", f"arn:aws:iam::{A}:user/x").allowed)
        guard = scenario(ALICE, [pol(stmt("Allow", "s3:*")), pol({"Effect": "Deny", "Action": "s3:*", "NotResource": "arn:aws:s3:::cc-data/*"})])
        self.assertTrue(decide(guard, "s3:GetObject", "arn:aws:s3:::cc-data/x").allowed)
        self.assertEqual(decide(guard, "s3:GetObject", "arn:aws:s3:::other/x").decision, "explicitDeny")

    def test_boundary_and_session_policies_intersect(self):
        sc = scenario(READER, [pol(stmt("Allow", "s3:*"))], boundary=main.make_policy("boundary", "b", pol(stmt("Allow", ["s3:Get*", "s3:List*"]))), session=[main.make_policy("session", "s", pol(stmt("Allow", "s3:List*")))])
        self.assertTrue(decide(sc, "s3:ListBucket", "arn:aws:s3:::cc-data").allowed)
        self.assertEqual(decide(sc, "s3:GetObject", "arn:aws:s3:::cc-data/a").kind, "session")
        self.assertEqual(decide(sc, "s3:PutObject", "arn:aws:s3:::cc-data/a").kind, "boundary")

    def test_every_scp_level_must_allow(self):
        full = main.make_policy("scp", "FullAWSAccess", pol(stmt("Allow", "*")))
        only_s3 = main.make_policy("scp", "OnlyS3", pol(stmt("Allow", "s3:*")))
        sc = scenario(ALICE, [pol(stmt("Allow", "*"))], scps=[[full], [only_s3, full]])
        self.assertTrue(decide(sc, "ec2:RunInstances").allowed)
        sc.scps = [[full], [only_s3]]
        self.assertEqual(decide(sc, "ec2:RunInstances").kind, "scp")
        self.assertTrue(decide(sc, "s3:ListAllMyBuckets").allowed)

    def test_scp_condition_with_ifexists_and_negation(self):
        region = main.make_policy("scp", "DenyOutsideEU", pol(stmt("Deny", "*", Condition={"StringNotEqualsIfExists": {"aws:RequestedRegion": ["eu-central-1", "eu-west-1"]}})))
        sc = scenario(ALICE, [pol(stmt("Allow", "*"))], scps=[[main.make_policy("scp", "Full", pol(stmt("Allow", "*"))), region]])
        self.assertEqual(decide(sc, "ec2:RunInstances", **{"aws:RequestedRegion": "us-east-1"}).decision, "explicitDeny")
        self.assertTrue(decide(sc, "ec2:RunInstances", **{"aws:RequestedRegion": "eu-central-1"}).allowed)
        self.assertEqual(decide(sc, "iam:ListUsers").decision, "explicitDeny")

    def test_same_account_resource_policy_grants_depend_on_the_principal_named(self):
        bucket = lambda who: main.make_policy("resource", "bucket", pol({"Effect": "Allow", "Principal": {"AWS": who}, "Action": "s3:GetObject", "Resource": "arn:aws:s3:::cc-logs/*"}))
        boundary = main.make_policy("boundary", "ec2-only", pol(stmt("Allow", "ec2:*")))
        self.assertEqual(decide(scenario(ERIN, boundary=boundary, resource_policy=bucket(ERIN)), "s3:GetObject", "arn:aws:s3:::cc-logs/a").decision, "allowed")
        self.assertEqual(decide(scenario(READER, boundary=boundary, resource_policy=bucket(f"arn:aws:iam::{A}:role/reader")), "s3:GetObject", "arn:aws:s3:::cc-logs/a").kind, "boundary")
        self.assertEqual(decide(scenario(ERIN, resource_policy=bucket(A)), "s3:GetObject", "arn:aws:s3:::cc-logs/a").kind, "identity")

    def test_cross_account_needs_both_sides(self):
        other = "arn:aws:iam::444455556666:user/partner"
        bucket = main.make_policy("resource", "bucket", pol({"Effect": "Allow", "Principal": {"AWS": "444455556666"}, "Action": "s3:GetObject", "Resource": "arn:aws:s3:::cc-data/*"}))
        allows = pol(stmt("Allow", "s3:GetObject", "arn:aws:s3:::cc-data/*"))
        both = decide(scenario(other, [allows], resource_policy=bucket, resource_account=A), "s3:GetObject", "arn:aws:s3:::cc-data/a")
        self.assertTrue(both.allowed and both.cross_account)
        self.assertEqual(decide(scenario(other, [allows], resource_account=A), "s3:GetObject", "arn:aws:s3:::cc-data/a").kind, "resource")
        self.assertEqual(decide(scenario(other, resource_policy=bucket, resource_account=A), "s3:GetObject", "arn:aws:s3:::cc-data/a").kind, "identity")

    def test_assume_role_needs_the_trust_policy_even_in_the_same_account(self):
        trust = main.make_policy("trust", "role/deploy", pol({"Effect": "Allow", "Principal": {"AWS": A}, "Action": "sts:AssumeRole"}))
        allows = pol(stmt("Allow", "sts:AssumeRole"))
        role = f"arn:aws:iam::{A}:role/deploy"
        self.assertTrue(decide(scenario(ALICE, [allows], resource_policy=trust), "sts:AssumeRole", role).allowed)
        self.assertEqual(decide(scenario(ALICE, resource_policy=trust), "sts:AssumeRole", role).kind, "identity")
        self.assertEqual(decide(scenario(ALICE, [allows]), "sts:AssumeRole", role).kind, "trust")

    def test_root_user_and_anonymous(self):
        self.assertTrue(decide(scenario(f"arn:aws:iam::{A}:root"), "s3:DeleteBucket", "arn:aws:s3:::cc-data").allowed)
        public = main.make_policy("resource", "public", pol({"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::cc-site/*"}))
        anonymous = scenario("anonymous", resource_policy=public, resource_account=A)
        self.assertTrue(decide(anonymous, "s3:GetObject", "arn:aws:s3:::cc-site/index.html").allowed)
        self.assertEqual(decide(anonymous, "s3:PutObject", "arn:aws:s3:::cc-site/index.html").kind, "resource")

    def test_missing_context_keys_are_reported(self):
        sc = scenario(ALICE, [pol(stmt("Allow", "s3:GetObject", Condition={"IpAddress": {"aws:SourceIp": "192.0.2.0/24"}}))])
        decision = decide(sc, "s3:GetObject", "arn:aws:s3:::cc-data/a")
        self.assertEqual((decision.decision, decision.missing), ("implicitDeny", ["aws:SourceIp"]))
        self.assertTrue(decide(sc, "s3:GetObject", "arn:aws:s3:::cc-data/a", **{"aws:SourceIp": "192.0.2.10"}).allowed)

    def test_every_documented_scenario(self):
        code, out, err = run_cli("scenarios", os.path.join(FIXTURES, "aws-doc-scenarios.json"), "--json")
        rows = json.loads(out)
        self.assertEqual(code, 0, [r for r in rows if not r["ok"]])
        self.assertGreaterEqual(len(rows), 40)
        self.assertTrue(all(r["source"].startswith("https://docs.aws.amazon.com/") for r in rows))


class AccountDumpTests(unittest.TestCase):
    def test_fixture_policies_match_the_policy_files(self):
        dump = fixture("account.json")
        users = {u["UserName"]: u for u in dump["UserDetailList"]}
        self.assertEqual(users["alice"]["UserPolicyList"][0]["PolicyDocument"], policy_file("alice-deny-secret.json"))
        self.assertEqual(users["carol"]["UserPolicyList"][0]["PolicyDocument"], policy_file("carol-home.json"))
        roles = {r["RoleName"]: r for r in dump["RoleDetailList"]}
        self.assertEqual(roles["reader"]["AssumeRolePolicyDocument"], policy_file("reader-trust.json"))
        self.assertEqual(dump["Policies"][0]["PolicyVersionList"][0]["Document"], policy_file("read-data.json"))

    def test_user_policies_come_from_inline_groups_and_managed_policies(self):
        decision, warnings = from_account(ALICE, "s3:GetObject", "arn:aws:s3:::cc-data/public/a.txt")
        self.assertEqual(warnings, [])
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.match.policy.via, "via group readers")
        denied, _ = from_account(ALICE, "s3:GetObject", "arn:aws:s3:::cc-data/secret/b.txt")
        self.assertEqual((denied.decision, denied.match.policy.name), ("explicitDeny", "user/alice/deny-secret"))

    def test_role_sessions_get_role_policies_boundary_and_tags(self):
        decision, _ = from_account(READER, "s3:GetObject", "arn:aws:s3:::cc-data/public/a.txt")
        self.assertEqual(decision.kind, "boundary")
        self.assertEqual(decision.context.get("aws:PrincipalTag/team"), ["data"])
        listed, _ = from_account(READER, "s3:ListBucket", "arn:aws:s3:::cc-data")
        self.assertTrue(listed.allowed)
        session = [main.make_policy("session", "list-only", policy_file("session-list-only.json"))]
        self.assertEqual(from_account(AUDITOR, "s3:GetObject", "arn:aws:s3:::cc-data/public/a.txt", session=session)[0].kind, "session")

    def test_assume_role_reads_the_trust_policy_from_the_dump(self):
        role = f"arn:aws:iam::{A}:role/reader"
        self.assertTrue(from_account(ALICE, "sts:AssumeRole", role, **{"sts:ExternalId": "cc-42"})[0].allowed)
        self.assertEqual(from_account(ALICE, "sts:AssumeRole", role, **{"sts:ExternalId": "wrong"})[0].kind, "trust")
        self.assertEqual(from_account(ALICE, "sts:AssumeRole", role)[0].kind, "trust")
        self.assertEqual(from_account(BOB, "sts:AssumeRole", f"arn:aws:iam::{A}:role/auditor")[0].kind, "identity")

    def test_policy_variables_in_the_dump(self):
        self.assertTrue(from_account(CAROL, "s3:PutObject", "arn:aws:s3:::cc-data/home/carol/x.txt")[0].allowed)
        self.assertFalse(from_account(CAROL, "s3:ListBucket", "arn:aws:s3:::cc-data", **{"s3:prefix": "home/bob/"})[0].allowed)
        self.assertTrue(from_account(CAROL, "s3:ListBucket", "arn:aws:s3:::cc-data", **{"s3:prefix": "home/carol/"})[0].allowed)

    def test_dump_without_policy_names_and_with_missing_policies(self):
        dump = fixture("account.json")
        for item in dump["Policies"]:
            item.pop("PolicyName")
        dump["Policies"] = dump["Policies"][:1]
        directory = main.load_directory(dump)
        self.assertEqual(directory.account, A)
        _, boundary, warnings = main.directory_policies(directory, main.parse_principal(READER))
        self.assertIsNone(boundary)
        self.assertIn("ListOnly", warnings[0])
        with self.assertRaises(main.PolicyError):
            main.load_directory({"Statement": []})


class ValidateTests(unittest.TestCase):
    def codes(self, document, kind="identity"):
        return {f.code for f in main.validate_policy(document, kind)}

    def test_structural_errors(self):
        codes = self.codes(pol({"Effect": "allow", "Action": "s3:GetObject", "NotAction": "s3:PutObject", "Principal": "*"}))
        self.assertTrue({"invalid-effect", "action-xor-notaction", "missing-resource", "principal-not-allowed"} <= codes)
        self.assertIn("missing-principal", self.codes(pol(stmt("Allow", "s3:GetObject")), "resource"))
        self.assertIn("invalid-condition-operator", self.codes(pol(stmt("Allow", "s3:*", Condition={"StringEqual": {"k": "v"}}))))
        self.assertIn("invalid-action", self.codes(pol(stmt("Allow", "GetObject"))))

    def test_security_warnings_and_warnings(self):
        self.assertIn("allow-all", self.codes(pol(stmt("Allow", "*"))))
        self.assertIn("allow-with-notaction", self.codes(policy_file("bob-poweruser.json")))
        self.assertIn("public-access", self.codes(pol({"Effect": "Allow", "Principal": {"AWS": "*"}, "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b/*"}), "resource"))
        self.assertIn("variables-are-literal", self.codes(pol(stmt("Allow", "s3:*", "arn:aws:s3:::b/${aws:username}/*"), version="2008-10-17")))
        tags = pol(stmt("Allow", "ec2:CreateTags", Condition={"ForAllValues:StringEquals": {"aws:TagKeys": ["env"]}, "ForAnyValue:StringEquals": {"aws:PrincipalTag/team": "data"}}))
        self.assertTrue({"forallvalues-without-null", "set-operator-on-single-valued-key"} <= self.codes(tags))
        self.assertIn("deleted-principal", self.codes(pol({"Effect": "Allow", "Principal": {"AWS": "AROAEXAMPLEREADER001"}, "Action": "sts:AssumeRole"}), "trust"))
        self.assertEqual(self.codes(policy_file("read-data.json")), set())


class ExplainAndCliTests(unittest.TestCase):
    def test_explain_documented_messages(self):
        scp = main.explain_message("User: arn:aws:iam::123456789012:user/John is not authorized to perform: codecommit:ListRepositories\nwith an explicit deny in a service control policy: arn:aws:organizations::777788889999:policy/o-exampleorgid/service_control_policy/p-examplepolicyid123")
        self.assertEqual((scp["decision"], scp["policy_type"], scp["action"]), ("explicitDeny", "service control policy", "codecommit:ListRepositories"))
        self.assertTrue(scp["policy_arn"].endswith("p-examplepolicyid123"))
        boundary = main.explain_message("User: arn:aws:iam::123456789012:user/John is not authorized to perform: codedeploy:ListDeployments on resource: arn:aws:codedeploy:us-east-1:123456789012:deploymentgroup:* because no permissions boundary allows the codedeploy:ListDeployments action")
        self.assertEqual((boundary["decision"], boundary["resource"]), ("implicitDeny", "arn:aws:codedeploy:us-east-1:123456789012:deploymentgroup:*"))
        self.assertIn("PermissionsBoundary", boundary["next"])

    def test_explain_a_message_without_a_reason_and_a_cloudtrail_event(self):
        moto = main.explain_message("An error occurred (AccessDenied) when calling the ListUsers operation: User: arn:aws:iam::123456789012:user/alice is not authorized to perform: iam:ListUsers")
        self.assertEqual((moto["decision"], moto["principal_type"], moto["policy_type"]), ("unknown", "User", None))
        event = {"eventSource": "s3.amazonaws.com", "eventName": "GetObject", "errorCode": "AccessDenied", "errorMessage": f"User: {READER} is not authorized to perform: s3:GetObject on resource: \"arn:aws:s3:::cc-data/a\" because no session policy allows the s3:GetObject action"}
        result = main.explain_message(json.dumps(event))
        self.assertEqual((result["policy_type"], result["resource"], result["event"]["eventName"]), ("session policy", "arn:aws:s3:::cc-data/a", "GetObject"))
        self.assertFalse(main.explain_message("Access Denied")["recognized"])

    def test_cli_eval_exit_codes_and_json(self):
        args = ["eval", "--principal", ALICE, "--auth-details", ACCOUNT, "--action", "s3:GetObject", "--time", "2026-11-01T12:00:00Z"]
        code, out, _ = run_cli(*args, "--resource", "arn:aws:s3:::cc-data/public/a.txt")
        self.assertEqual(code, 0)
        self.assertIn('statement "ReadData"', out)
        code, out, _ = run_cli(*args, "--resource", "arn:aws:s3:::cc-data/public/a.txt", "--resource", "arn:aws:s3:::cc-data/secret/b.txt", "--json")
        results = json.loads(out)["EvaluationResults"]
        self.assertEqual(code, 1)
        self.assertEqual([r["EvalDecision"] for r in results], ["allowed", "explicitDeny"])
        self.assertEqual(results[1]["DecidingStatement"]["Sid"], "NoSecrets")
        self.assertEqual(results[0]["Context"]["aws:CurrentTime"], "2026-11-01T12:00:00Z")

    def test_cli_inline_policies_resource_policy_and_errors(self):
        code, out, _ = run_cli("eval", "--principal", ERIN, "--action", "s3:GetObject", "--resource", "arn:aws:s3:::cc-logs/a", "--resource-policy", os.path.join(POLICIES, "logs-bucket-policy.json"), "--json")
        self.assertEqual((code, json.loads(out)["EvaluationResults"][0]["DecidingPolicyType"]), (0, "resource-based policy"))
        code, out, _ = run_cli("eval", "--principal", ERIN, "--action", "s3:GetObject", "--resource", "arn:aws:s3:::cc-logs/a", "--resource-policy", os.path.join(POLICIES, "logs-bucket-policy.json"), "-c", "aws:SecureTransport=false")
        self.assertEqual(code, 1)
        self.assertIn("TlsOnly", out)
        code, _, err = run_cli("eval", "--principal", ALICE, "--action", "s3:GetObject", "--policy", "{not json")
        self.assertEqual(code, 2)
        self.assertIn("not JSON", err)
        code, _, err = run_cli("eval", "--principal", ALICE, "--action", "s3:GetObject", "-c", "novalue")
        self.assertEqual(code, 2)
        self.assertIn("KEY=VALUE", err)

    def test_cli_validate_and_explain(self):
        code, out, _ = run_cli("validate", os.path.join(POLICIES, "bob-poweruser.json"))
        self.assertEqual(code, 1)
        self.assertIn("allow-with-notaction", out)
        self.assertEqual(run_cli("validate", os.path.join(POLICIES, "reader-trust.json"), "--type", "trust")[0], 0)
        code, out, _ = run_cli("explain", f"User: {ALICE} is not authorized to perform: iam:ListUsers because no identity-based policy allows the iam:ListUsers action", "--json")
        self.assertEqual((code, json.loads(out)["decision"]), (0, "implicitDeny"))


if __name__ == "__main__":
    unittest.main()
