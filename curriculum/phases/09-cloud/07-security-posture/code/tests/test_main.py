import base64
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "moto-bundle.json")
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
ACCOUNT = "123456789012"
ALL_ON = {flag: True for flag in main.BPA_FLAGS}
ALL_OFF = {flag: False for flag in main.BPA_FLAGS}
REPORT_HEADER = "user,arn,user_creation_time,password_enabled,password_last_used,password_last_changed,password_next_rotation,mfa_active,access_key_1_active,access_key_1_last_rotated,access_key_1_last_used_date,access_key_1_last_used_region,access_key_1_last_used_service,access_key_2_active,access_key_2_last_rotated,access_key_2_last_used_date,access_key_2_last_used_region,access_key_2_last_used_service,cert_1_active,cert_1_last_rotated,cert_2_active,cert_2_last_rotated"


def ok(output):
    return {"ok": True, "output": output}


def err(code, message=""):
    return {"ok": False, "error": code, "message": message}


def make_bundle(calls):
    return {"posture_bundle": 1, "account": ACCOUNT, "region": "us-east-1", "collected_at": main.iso(NOW), "calls": calls}


def run_scan(calls, now=NOW, **settings):
    return main.scan_bundle(make_bundle(calls), main.Settings(now, **settings))


def found(scan, check=None):
    return sorted((f.check.id, f.name) for f in scan.findings if check is None or f.check.id == check)


def fixture():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


def cli(*argv):
    out, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(errors):
        code = main.main(list(argv))
    return code, out.getvalue(), errors.getvalue()


def bucket_calls(name, bpa=None, acl_grants=(), policy=None):
    calls = {
        f"s3api get-public-access-block --bucket {name}": ok({"PublicAccessBlockConfiguration": bpa}) if bpa is not None else err(main.NO_BPA),
        f"s3api get-bucket-acl --bucket {name}": ok({"Grants": [{"Grantee": {"Type": "Group", "URI": uri}, "Permission": permission} for uri, permission in acl_grants]}),
        f"s3api get-bucket-policy --bucket {name}": ok({"Policy": json.dumps(policy)}) if policy is not None else err(main.NO_POLICY),
    }
    return calls


def s3_calls(buckets, account_bpa=None):
    calls = {"s3api list-buckets": ok({"Buckets": [{"Name": name} for name in buckets]}),
             f"s3control get-public-access-block --account-id {ACCOUNT}": ok({"PublicAccessBlockConfiguration": account_bpa}) if account_bpa is not None else err(main.NO_BPA)}
    for name, spec in buckets.items():
        calls.update(bucket_calls(name, **spec))
    return calls


def report(*rows):
    text = REPORT_HEADER + "\n" + "\n".join(rows) + "\n"
    return ok({"Content": base64.b64encode(text.encode()).decode(), "ReportFormat": "text/csv"})


def row(user, password="false", password_used="N/A", mfa="false", key1="false", key1_used="N/A", created="2025-01-01T00:00:00+00:00"):
    fields = [user, f"arn:aws:iam::{ACCOUNT}:{'root' if user == main.ROOT_ROW else 'user/' + user}", created, password, password_used, "N/A", "N/A", mfa,
              key1, "N/A", key1_used, "N/A", "N/A", "false", "N/A", "N/A", "N/A", "N/A", "false", "N/A", "false", "N/A"]
    return ",".join(fields)


def iso_days_ago(days):
    return main.iso(NOW - timedelta(days=days))


class FixtureTests(unittest.TestCase):
    def test_the_moto_scenario_produces_exactly_the_expected_findings(self):
        scan = main.scan_bundle(fixture(), main.Settings(main.resolve_now("+120d", fixture())))
        named = {(f.check.id, f.name) for f in scan.findings if f.name.startswith("cc-") or f.name in ("account", "root")}
        self.assertEqual(named, {
            ("s3-public-acl", "cc-public-acl"), ("s3-public-policy", "cc-public-policy"), ("s3-bucket-bpa-off", "cc-public-acl"), ("s3-bucket-bpa-off", "cc-public-policy"),
            ("s3-public-policy", "cc-vpc-wildcard"), ("s3-bucket-bpa-off", "cc-vpc-wildcard"), ("s3-tls-not-enforced", "cc-vpc-wildcard"),
            ("s3-account-bpa-off", "account"), ("s3-tls-not-enforced", "cc-public-acl"), ("s3-tls-not-enforced", "cc-public-policy"),
            ("sg-admin-ipv4", "cc-ssh-open"), ("sg-admin-ipv4", "cc-all-open"), ("sg-admin-ipv6", "cc-rdp-open-v6"), ("default-sg-open", "cc-legacy"),
            ("nacl-admin-open", "cc-admin-open"), ("nacl-admin-open", "cc-wide-range"), ("ebs-unencrypted", "cc-data-plain"), ("ebs-default-encryption-off", "account"),
            ("ec2-imdsv1", "cc-imdsv1"), ("root-mfa-off", "root"), ("user-console-no-mfa", "cc-alice"), ("user-two-active-keys", "cc-ci"),
            ("user-key-old", "cc-alice"), ("user-key-old", "cc-ci"), ("user-unused-credential", "cc-alice"), ("user-unused-credential", "cc-bob"), ("user-unused-credential", "cc-ci"),
            ("password-policy-length", "account"), ("password-policy-reuse", "account"), ("cloudtrail-no-multi-region", "account"),
            ("cloudtrail-no-validation", "cc-trail"), ("cloudtrail-no-kms", "cc-trail"), ("config-not-recording", "account"), ("kms-rotation-off", "cc-no-rotation"),
            ("vpc-flow-logs-off", "cc-legacy"),
        })
        self.assertNotIn(("nacl-admin-open", "cc-ssh-denied-first"), named)
        self.assertNotIn(("sg-admin-ipv4", "cc-web"), named)
        self.assertEqual(scan.gaps, [{"call": "iam get-credential-report", "reason": "no <root_account> row: root-used not evaluated"}])

    def test_the_clock_is_an_input_so_fresh_keys_are_not_old(self):
        scan = main.scan_bundle(fixture(), main.Settings(main.resolve_now("", fixture())))
        self.assertEqual(found(scan, "user-key-old") + found(scan, "user-unused-credential"), [])
        self.assertEqual(main.resolve_now("2027-01-01", fixture()).date().isoformat(), "2027-01-01")
        with self.assertRaises(main.PostureError):
            main.resolve_now("next week", fixture())

    def test_cli_exit_codes_severity_filter_and_benchmark_numbering(self):
        code, out, _ = cli("scan", FIXTURE)
        self.assertEqual(code, 1)
        self.assertIn("sg-admin-ipv4               cc-all-open               CIS 5.3 [EC2.53]", out)
        code, out, _ = cli("scan", FIXTURE, "--benchmark", "3.0.0")
        self.assertIn("CIS 5.2 [EC2.53]", out)
        self.assertIn("CIS 2.2.1 [EC2.7]", out)
        self.assertEqual(cli("scan", FIXTURE, "--fail-on", "none")[0], 0)
        code, out, _ = cli("scan", FIXTURE, "--min-severity", "critical", "--json")
        doc = json.loads(out)
        self.assertEqual({f["severity"] for f in doc["findings"]}, {"critical"})
        self.assertEqual(doc["findings"][0]["cis"].keys(), {"5.0.0", "3.0.0"})

    def test_every_check_maps_to_a_known_severity_and_both_benchmarks_or_neither(self):
        for check in main.CHECKS.values():
            self.assertIn(check.severity, main.SEVERITIES)
            self.assertEqual(bool(check.cis5), bool(check.cis3), check.id)
        self.assertEqual((main.CHECKS["root-mfa-off"].cis5, main.CHECKS["root-mfa-off"].cis3), ("1.4", "1.5"))
        self.assertEqual((main.CHECKS["ebs-default-encryption-off"].cis5, main.CHECKS["ebs-default-encryption-off"].cis3), ("5.1.1", "2.2.1"))


class S3Tests(unittest.TestCase):
    def test_block_public_access_is_bucket_or_account_and_neutralises_a_public_acl(self):
        public_read = [("http://acs.amazonaws.com/groups/global/AllUsers", "READ")]
        scan = run_scan(s3_calls({"site": {"bpa": None, "acl_grants": public_read}}, account_bpa=ALL_ON))
        self.assertEqual(found(scan, "s3-bucket-bpa-off"), [])
        self.assertEqual([(f.check.id, f.severity) for f in scan.findings if f.check.id == "s3-public-acl"], [("s3-public-acl", "low")])
        scan = run_scan(s3_calls({"site": {"bpa": dict(ALL_ON, IgnorePublicAcls=False), "acl_grants": public_read}}))
        self.assertEqual([(f.check.id, f.severity) for f in scan.findings if f.check.id.startswith("s3-public")], [("s3-public-acl", "critical")])
        self.assertEqual(next(f for f in scan.findings if f.check.id == "s3-bucket-bpa-off").details["off"], ["IgnorePublicAcls"])
        self.assertEqual(found(scan, "s3-account-bpa-off"), [("s3-account-bpa-off", "account")])

    def test_authenticated_users_is_public_too(self):
        scan = run_scan(s3_calls({"logs": {"bpa": ALL_OFF, "acl_grants": [("http://acs.amazonaws.com/groups/global/AuthenticatedUsers", "WRITE")]}}, account_bpa=ALL_OFF))
        finding = next(f for f in scan.findings if f.check.id == "s3-public-acl")
        self.assertIn("AuthenticatedUsers (anyone with any AWS account) has WRITE", finding.evidence[0])

    def test_the_documented_example_policies(self):
        base = {"Principal": "*", "Resource": "*", "Action": "s3:PutObject", "Effect": "Allow"}
        self.assertTrue(main.statement_is_public(base))
        self.assertTrue(main.statement_is_public(dict(base, Condition={"StringLike": {"aws:SourceVpc": "vpc-*"}})))
        self.assertFalse(main.statement_is_public(dict(base, Condition={"StringEquals": {"aws:SourceVpc": "vpc-91237329"}})))
        self.assertTrue(main.statement_is_public(dict(base, Condition={"StringEquals": {"aws:SourceVpc": "${aws:username}"}})))
        self.assertTrue(main.statement_is_public(dict(base, Condition={"StringEqualsIfExists": {"aws:SourceVpce": "vpce-1a2b3c4d"}})))
        self.assertTrue(main.statement_is_public(dict(base, Condition={"StringNotEquals": {"aws:PrincipalOrgID": "o-abc"}})))
        self.assertFalse(main.statement_is_public(dict(base, Condition={"StringEquals": {"aws:PrincipalOrgID": "o-abc123"}})))
        self.assertFalse(main.statement_is_public(dict(base, Effect="Deny")))

    def test_source_ip_must_be_narrower_than_slash_8_for_ipv4_and_slash_32_for_ipv6(self):
        def public(op, cidrs):
            return main.statement_is_public({"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": "*", "Condition": {op: {"aws:SourceIp": cidrs}}})
        self.assertTrue(public("IpAddress", "0.0.0.0/1"))
        self.assertFalse(public("IpAddress", ["203.0.113.0/24", "198.51.100.7/32"]))
        self.assertFalse(public("IpAddress", "10.0.0.0/8"))
        self.assertTrue(public("IpAddress", ["203.0.113.0/24", "0.0.0.0/0"]))
        self.assertTrue(public("IpAddress", "2001:db8::/16"))
        self.assertFalse(public("IpAddress", "2001:db8:1234::/48"))
        self.assertTrue(public("NotIpAddress", "203.0.113.0/24"))

    def test_principal_forms(self):
        def stmt(**kwargs):
            return dict({"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}, **kwargs)
        self.assertTrue(main.statement_is_public(stmt(Principal={"AWS": "*"})))
        self.assertTrue(main.statement_is_public(stmt(Principal={"AWS": ["arn:aws:iam::111122223333:root", "arn:aws:iam::*:root"]})))
        self.assertTrue(main.statement_is_public(stmt(NotPrincipal={"AWS": "arn:aws:iam::111122223333:root"})))
        self.assertFalse(main.statement_is_public(stmt(Principal={"AWS": "arn:aws:iam::111122223333:root"})))
        self.assertFalse(main.statement_is_public(stmt(Principal={"Service": "cloudtrail.amazonaws.com"})))

    def test_tls_enforcement_is_an_explicit_deny_for_every_principal_and_both_resource_shapes(self):
        arn = "arn:aws:s3:::data"
        deny = {"Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": [arn, arn + "/*"], "Condition": {"Bool": {"aws:SecureTransport": "false"}}}
        self.assertIsNone(main.tls_gap({"Statement": [deny]}, "data"))
        self.assertIsNone(main.tls_gap({"Statement": [dict(deny, Condition={"BoolIfExists": {"aws:SecureTransport": "false"}})]}, "data"))
        self.assertIn("s3:ListBucket", main.tls_gap({"Statement": [dict(deny, Resource=arn + "/*")]}, "data"))
        self.assertIn("s3:GetObject", main.tls_gap({"Statement": [dict(deny, Principal={"AWS": "arn:aws:iam::111122223333:root"})]}, "data"))
        self.assertIn("s3:PutObject", main.tls_gap({"Statement": [dict(deny, Action="s3:GetObject")]}, "data"))
        self.assertIsNotNone(main.tls_gap({}, "data"))

    def test_condition_semantics_for_missing_keys(self):
        self.assertFalse(main.condition_holds("StringEquals", "aws:SourceVpc", ["vpc-1"], {}))
        self.assertTrue(main.condition_holds("StringNotEquals", "aws:SourceVpc", ["vpc-1"], {}))
        self.assertTrue(main.condition_holds("StringEqualsIfExists", "aws:SourceVpc", ["vpc-1"], {}))
        self.assertTrue(main.condition_holds("IpAddress", "aws:SourceIp", ["203.0.113.0/24"], {"aws:sourceip": ["203.0.113.9"]}))
        self.assertIsNone(main.condition_holds("DateGreaterThan", "aws:CurrentTime", ["2020-01-01"], {"aws:currenttime": ["2026-01-01"]}))

    def test_malformed_policy_becomes_a_gap_not_a_crash(self):
        calls = s3_calls({"broken": {"bpa": ALL_ON}}, account_bpa=ALL_ON)
        calls["s3api get-bucket-policy --bucket broken"] = ok({"Policy": "{not json"})
        scan = run_scan(calls)
        self.assertIn({"call": "s3api get-bucket-policy --bucket broken", "reason": "Policy is not valid JSON"}, scan.gaps)


def sg(gid, name, ingress=(), egress=(), vpc="vpc-1"):
    return {"GroupId": gid, "GroupName": name, "VpcId": vpc, "IpPermissions": list(ingress), "IpPermissionsEgress": list(egress)}


def perm(protocol, low=None, high=None, v4=(), v6=()):
    p = {"IpProtocol": protocol, "IpRanges": [{"CidrIp": c} for c in v4], "Ipv6Ranges": [{"CidrIpv6": c} for c in v6], "UserIdGroupPairs": [], "PrefixListIds": []}
    if low is not None:
        p.update(FromPort=low, ToPort=high)
    return p


def nacl(acl_id, entries, default=False):
    return {"NetworkAclId": acl_id, "IsDefault": default, "Tags": [{"Key": "Name", "Value": acl_id}], "Entries": entries}


def entry(number, action, protocol="6", low=None, high=None, cidr="0.0.0.0/0", ipv6=False):
    e = {"RuleNumber": number, "RuleAction": action, "Protocol": protocol, "Egress": False, ("Ipv6CidrBlock" if ipv6 else "CidrBlock"): cidr}
    if low is not None:
        e["PortRange"] = {"From": low, "To": high}
    return e


class NetworkTests(unittest.TestCase):
    def scan_groups(self, *groups):
        return run_scan({"ec2 describe-vpcs": ok({"Vpcs": [{"VpcId": "vpc-1", "Tags": [{"Key": "Name", "Value": "app"}]}]}), "ec2 describe-security-groups": ok({"SecurityGroups": list(groups)})})

    def test_admin_ports_cover_all_traffic_ranges_and_udp_but_not_other_ports(self):
        scan = self.scan_groups(
            sg("sg-all", "all", [perm("-1", v4=["0.0.0.0/0"])]),
            sg("sg-range", "range", [perm("tcp", 20, 30, v4=["0.0.0.0/0"])]),
            sg("sg-udp", "udp", [perm("udp", 3389, 3389, v6=["::/0"])]),
            sg("sg-web", "web", [perm("tcp", 443, 443, v4=["0.0.0.0/0"]), perm("icmp", -1, -1, v4=["0.0.0.0/0"])]),
            sg("sg-office", "office", [perm("tcp", 22, 22, v4=["203.0.113.0/24"])]),
        )
        self.assertEqual(found(scan, "sg-admin-ipv4"), [("sg-admin-ipv4", "all"), ("sg-admin-ipv4", "range")])
        self.assertEqual(found(scan, "sg-admin-ipv6"), [("sg-admin-ipv6", "udp")])
        ports = {f.name: f.details["ports"] for f in scan.findings}
        self.assertEqual(ports, {"all": [22, 3389], "range": [22], "udp": [3389]})

    def test_revoke_command_removes_only_the_world_range(self):
        scan = self.scan_groups(sg("sg-ssh", "ssh", [perm("tcp", 22, 22, v4=["203.0.113.0/24", "0.0.0.0/0"])]))
        command = scan.findings[0].commands[0]
        self.assertEqual(command[:4], ["ec2", "revoke-security-group-ingress", "--group-id", "sg-ssh"])
        self.assertEqual(json.loads(command[5]), [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])

    def test_default_group_with_rules_is_reported_by_vpc_name_with_both_revokes(self):
        scan = self.scan_groups(sg("sg-d", "default", [perm("-1", v4=[])], [perm("-1", v4=["0.0.0.0/0"])]), sg("sg-e", "default", vpc="vpc-2"))
        self.assertEqual(found(scan, "default-sg-open"), [("default-sg-open", "app")])
        self.assertEqual([c[1] for c in scan.findings[0].commands], ["revoke-security-group-ingress", "revoke-security-group-egress"])

    def test_nacl_rules_are_evaluated_in_order_like_the_vpc_does(self):
        acls = [
            nacl("acl-open", [entry(100, "allow", "6", 22, 22), entry(32767, "deny", "-1")]),
            nacl("acl-denied-first", [entry(100, "allow", "6", 22, 22), entry(90, "deny", "6", 22, 22)]),
            nacl("acl-narrow-deny", [entry(50, "deny", "6", 22, 22, cidr="198.51.100.0/24"), entry(100, "allow", "6", 0, 65535)]),
            nacl("acl-default", [entry(100, "allow", "-1")], default=True),
            nacl("acl-v6-all", [entry(200, "allow", "-1", cidr="::/0", ipv6=True)]),
        ]
        scan = run_scan({"ec2 describe-network-acls": ok({"NetworkAcls": acls})})
        self.assertEqual(found(scan, "nacl-admin-open"), [("nacl-admin-open", n) for n in ("acl-narrow-deny", "acl-open", "acl-v6-all")])
        by_name = {f.name: f for f in scan.findings}
        self.assertEqual(by_name["acl-narrow-deny"].details, {"ports": [22, 3389], "rules": [100]})
        open_fix = by_name["acl-open"].commands
        self.assertEqual(len(open_fix), 1)
        self.assertEqual(open_fix[0][open_fix[0].index("--rule-number") + 1], "99")
        v6_fix = by_name["acl-v6-all"].commands
        self.assertEqual(sorted((c[c.index("--protocol") + 1], c[c.index("--port-range") + 1]) for c in v6_fix), [("17", "From=22,To=22"), ("17", "From=3389,To=3389"), ("6", "From=22,To=22"), ("6", "From=3389,To=3389")])
        self.assertTrue(all("--ipv6-cidr-block" in c for c in v6_fix))
        self.assertEqual(main.free_rule_numbers([entry(1, "allow"), entry(2, "deny")], 3, 1), [])

    def test_flow_logs_must_be_active_and_capture_rejects(self):
        calls = {
            "ec2 describe-vpcs": ok({"Vpcs": [{"VpcId": v} for v in ("vpc-all", "vpc-reject", "vpc-accept", "vpc-none")]}),
            "ec2 describe-flow-logs": ok({"FlowLogs": [{"ResourceId": "vpc-all", "TrafficType": "ALL", "FlowLogStatus": "ACTIVE"}, {"ResourceId": "vpc-reject", "TrafficType": "REJECT", "FlowLogStatus": "ACTIVE"}, {"ResourceId": "vpc-accept", "TrafficType": "ACCEPT", "FlowLogStatus": "ACTIVE"}]}),
        }
        self.assertEqual(found(run_scan(calls), "vpc-flow-logs-off"), [("vpc-flow-logs-off", "vpc-accept"), ("vpc-flow-logs-off", "vpc-none")])

    def test_access_denied_is_a_gap_and_the_other_checks_still_run(self):
        scan = run_scan({"ec2 describe-security-groups": err("UnauthorizedOperation", "not authorized"), "ec2 get-ebs-encryption-by-default": ok({"EbsEncryptionByDefault": False})})
        self.assertIn({"call": "ec2 describe-security-groups", "reason": "UnauthorizedOperation: not authorized"}, scan.gaps)
        self.assertEqual(found(scan, "ebs-default-encryption-off"), [("ebs-default-encryption-off", "account")])
        self.assertTrue(any(g["call"] == "s3api list-buckets" and g["reason"] == "not collected" for g in scan.gaps))


class ComputeTests(unittest.TestCase):
    def test_volume_severity_depends_on_attachment_and_imds_needs_tokens(self):
        calls = {
            "ec2 describe-volumes": ok({"Volumes": [
                {"VolumeId": "vol-1", "Encrypted": False, "State": "in-use", "Size": 8, "Attachments": [{"InstanceId": "i-1", "Device": "/dev/xvda"}]},
                {"VolumeId": "vol-2", "Encrypted": False, "State": "available", "Size": 8, "Tags": [{"Key": "Name", "Value": "spare"}]},
                {"VolumeId": "vol-3", "Encrypted": True, "State": "in-use", "Size": 8}]}),
            "ec2 describe-instances": ok({"Reservations": [{"Instances": [
                {"InstanceId": "i-1", "State": {"Name": "running"}, "MetadataOptions": {"HttpTokens": "optional", "HttpEndpoint": "enabled"}},
                {"InstanceId": "i-2", "State": {"Name": "running"}, "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled"}},
                {"InstanceId": "i-3", "State": {"Name": "stopped"}, "MetadataOptions": {"HttpTokens": "optional", "HttpEndpoint": "disabled"}},
                {"InstanceId": "i-4", "State": {"Name": "terminated"}, "MetadataOptions": {"HttpTokens": "optional"}}]}]}),
        }
        scan = run_scan(calls)
        self.assertEqual([(f.name, f.severity) for f in scan.findings if f.check.id == "ebs-unencrypted"], [("vol-1", "medium"), ("spare", "low")])
        self.assertEqual(found(scan, "ec2-imdsv1"), [("ec2-imdsv1", "i-1")])
        self.assertIn("--http-tokens", next(f for f in scan.findings if f.check.id == "ec2-imdsv1").commands[0])


class IdentityTests(unittest.TestCase):
    def test_root_row_drives_key_mfa_and_recent_use(self):
        calls = {"iam get-credential-report": report(row(main.ROOT_ROW, password="not_supported", password_used=iso_days_ago(3), key1="true", key1_used=iso_days_ago(200)))}
        scan = run_scan(calls)
        self.assertEqual(found(scan), [("root-access-key", "root"), ("root-mfa-off", "root"), ("root-used", "root")])
        used = next(f for f in scan.findings if f.check.id == "root-used")
        self.assertEqual(len(used.evidence), 1)
        self.assertIn("password_last_used", used.evidence[0])
        quiet = run_scan({"iam get-credential-report": report(row(main.ROOT_ROW, password="not_supported", password_used=iso_days_ago(120), mfa="true"))})
        self.assertEqual(found(quiet), [])

    def test_without_a_root_row_the_account_summary_answers_and_root_use_is_a_gap(self):
        scan = run_scan({"iam get-credential-report": report(row("alice")), "iam get-account-summary": ok({"SummaryMap": {"AccountMFAEnabled": 1, "AccountAccessKeysPresent": 1}})})
        self.assertEqual(found(scan), [("root-access-key", "root")])
        self.assertTrue(any("root-used not evaluated" in g["reason"] for g in scan.gaps))

    def test_user_thresholds_for_mfa_unused_and_old_keys(self):
        def keys(user, *specs):
            calls = {f"iam list-access-keys --user-name {user}": ok({"AccessKeyMetadata": [{"AccessKeyId": kid, "Status": status, "CreateDate": iso_days_ago(age)} for kid, status, age, _ in specs]})}
            for kid, _, _, used in specs:
                calls[f"iam get-access-key-last-used --access-key-id {kid}"] = ok({"AccessKeyLastUsed": {"LastUsedDate": iso_days_ago(used)} if used is not None else {"ServiceName": "N/A"}})
            return calls
        calls = {"iam list-users": ok({"Users": [{"UserName": u, "Arn": f"arn:aws:iam::{ACCOUNT}:user/{u}"} for u in ("busy", "fresh", "idle", "human")]}),
                 "iam get-credential-report": report(row("busy"), row("fresh"), row("idle"), row("human", password="true", password_used=iso_days_ago(10), mfa="false"))}
        calls.update(keys("busy", ("AKIABUSY", "Active", 100, 10)))
        calls.update(keys("fresh", ("AKIAFRESH", "Active", 30, None)))
        calls.update(keys("idle", ("AKIAIDLE", "Active", 50, None), ("AKIAOFF", "Inactive", 400, None)))
        calls.update(keys("human"))
        scan = run_scan(calls)
        self.assertEqual(found(scan), [("user-console-no-mfa", "human"), ("user-key-old", "busy"), ("user-unused-credential", "idle")])
        idle = next(f for f in scan.findings if f.check.id == "user-unused-credential")
        self.assertEqual(idle.commands, [["iam", "update-access-key", "--user-name", "idle", "--access-key-id", "AKIAIDLE", "--status", "Inactive"]])
        self.assertEqual(found(run_scan(calls, key_max_age_days=120)), [("user-console-no-mfa", "human"), ("user-unused-credential", "idle")])

    def test_password_policy_fix_keeps_the_settings_already_in_place(self):
        scan = run_scan({"iam get-account-password-policy": ok({"PasswordPolicy": {"MinimumPasswordLength": 16, "PasswordReusePrevention": 3, "RequireSymbols": True, "ExpirePasswords": True, "MaxPasswordAge": 90}})})
        self.assertEqual(found(scan), [("password-policy-reuse", "account")])
        self.assertEqual(scan.findings[0].commands[0], ["iam", "update-account-password-policy", "--minimum-password-length", "16", "--password-reuse-prevention", "24", "--require-symbols", "--max-password-age", "90"])
        missing = run_scan({"iam get-account-password-policy": err(main.NO_ENTITY)})
        self.assertEqual(found(missing), [("password-policy-length", "account"), ("password-policy-reuse", "account")])


class LoggingTests(unittest.TestCase):
    def trail_calls(self, trail, logging=True, selectors=None, ref=None):
        ref = ref or trail["Name"]
        return {"cloudtrail describe-trails": ok({"trailList": [trail]}),
                f"cloudtrail get-trail-status --name {ref}": ok({"IsLogging": logging}),
                f"cloudtrail get-event-selectors --trail-name {ref}": ok(selectors or {"EventSelectors": [], "AdvancedEventSelectors": []})}

    def test_multi_region_trail_must_be_logging_all_management_events(self):
        good = {"Name": "org", "TrailARN": "arn:aws:cloudtrail:us-east-1:1:trail/org", "HomeRegion": "us-east-1", "IsMultiRegionTrail": True, "LogFileValidationEnabled": True, "KmsKeyId": "k"}
        self.assertEqual(found(run_scan(self.trail_calls(good))), [])
        stopped = run_scan(self.trail_calls(good, logging=False))
        self.assertEqual(found(stopped), [("cloudtrail-no-multi-region", "account")])
        self.assertEqual(stopped.findings[0].commands, [["cloudtrail", "start-logging", "--name", "org", "--region", "us-east-1"]])
        write_only = {"EventSelectors": [{"ReadWriteType": "WriteOnly", "IncludeManagementEvents": True}]}
        self.assertEqual(found(run_scan(self.trail_calls(dict(good, HasCustomEventSelectors=True), selectors=write_only))), [("cloudtrail-no-multi-region", "account")])
        advanced = {"AdvancedEventSelectors": [{"FieldSelectors": [{"Field": "eventCategory", "Equals": ["Management"]}]}]}
        self.assertEqual(found(run_scan(self.trail_calls(dict(good, HasCustomEventSelectors=True), selectors=advanced))), [])
        shadow = dict(good, HomeRegion="eu-west-1")
        self.assertEqual(found(run_scan(self.trail_calls(shadow, ref=shadow["TrailARN"]))), [])

    def test_single_region_trail_gets_validation_kms_and_upgrade_commands(self):
        trail = {"Name": "local", "TrailARN": "arn:aws:cloudtrail:us-east-1:1:trail/local", "HomeRegion": "us-east-1", "IsMultiRegionTrail": False}
        scan = run_scan(self.trail_calls(trail))
        self.assertEqual(found(scan), [("cloudtrail-no-kms", "local"), ("cloudtrail-no-multi-region", "account"), ("cloudtrail-no-validation", "local")])
        upgrade = next(f for f in scan.findings if f.check.id == "cloudtrail-no-multi-region").commands
        self.assertEqual([c[1] for c in upgrade], ["update-trail", "start-logging"])

    def test_config_recorder_must_exist_record_and_cover_all_types(self):
        def scan_config(recorders, statuses):
            return found(run_scan({"configservice describe-configuration-recorders": ok({"ConfigurationRecorders": recorders}),
                                   "configservice describe-configuration-recorder-status": ok({"ConfigurationRecordersStatus": statuses})}))
        expected = [("config-not-recording", "account")]
        self.assertEqual(scan_config([], []), expected)
        self.assertEqual(scan_config([{"name": "default", "recordingGroup": {"allSupported": True}}], [{"name": "default", "recording": False}]), expected)
        self.assertEqual(scan_config([{"name": "default", "recordingGroup": {"allSupported": False}}], [{"name": "default", "recording": True}]), expected)
        self.assertEqual(scan_config([{"name": "default", "recordingGroup": {"allSupported": True}}], [{"name": "default", "recording": True}]), [])

    def test_kms_rotation_only_for_enabled_customer_symmetric_keys(self):
        keys = {"k-cust": ("CUSTOMER", "SYMMETRIC_DEFAULT", "Enabled", "cust"), "k-aws": ("CUSTOMER", "SYMMETRIC_DEFAULT", "Enabled", "aws/ebs"),
                "k-rsa": ("CUSTOMER", "RSA_2048", "Enabled", "signing"), "k-off": ("CUSTOMER", "SYMMETRIC_DEFAULT", "Disabled", "old")}
        calls = {"kms list-keys": ok({"Keys": [{"KeyId": k, "KeyArn": "arn:" + k} for k in keys]}),
                 "kms list-aliases": ok({"Aliases": [{"AliasName": "alias/" + alias, "TargetKeyId": k} for k, (_, _, _, alias) in keys.items()]})}
        for k, (manager, spec, state, _) in keys.items():
            calls[f"kms describe-key --key-id {k}"] = ok({"KeyMetadata": {"KeyId": k, "KeyManager": manager, "KeySpec": spec, "KeyState": state, "Origin": "AWS_KMS"}})
            calls[f"kms get-key-rotation-status --key-id {k}"] = ok({"KeyRotationEnabled": False})
        self.assertEqual(found(run_scan(calls)), [("kms-rotation-off", "cust")])


class CollectorTests(unittest.TestCase):
    def fake_runner(self, responses, seen):
        def run(argv):
            key = " ".join(argv)
            seen.append(key)
            value = responses.get(key, {})
            if isinstance(value, str):
                return main.CallResult(254, "", f"\nAn error occurred ({value}) when calling the Operation: details")
            return main.CallResult(0, json.dumps(value), "")
        return run

    def test_collect_follows_outputs_and_polls_the_credential_report(self):
        states = iter(["STARTED", "INPROGRESS", "COMPLETE"])
        responses = {
            "sts get-caller-identity": {"Account": ACCOUNT},
            "s3api list-buckets": {"Buckets": [{"Name": "b1"}]},
            "s3api get-public-access-block --bucket b1": "NoSuchPublicAccessBlockConfiguration",
            "cloudtrail describe-trails": {"trailList": [{"Name": "t1", "TrailARN": "arn:t1", "HomeRegion": "us-east-1"}, {"Name": "t2", "TrailARN": "arn:t2", "HomeRegion": "eu-west-1"}]},
            "iam list-users": {"Users": [{"UserName": "u1"}]},
            "iam list-access-keys --user-name u1": {"AccessKeyMetadata": [{"AccessKeyId": "AKIA1"}]},
            "kms list-keys": {"Keys": [{"KeyId": "k1"}, {"KeyId": "k2"}]},
            "kms describe-key --key-id k1": {"KeyMetadata": {"KeyManager": "CUSTOMER", "KeySpec": "SYMMETRIC_DEFAULT", "KeyState": "Enabled"}},
            "kms describe-key --key-id k2": {"KeyMetadata": {"KeyManager": "AWS", "KeySpec": "SYMMETRIC_DEFAULT", "KeyState": "Enabled"}},
        }
        seen, sleeps = [], []
        runner = self.fake_runner(responses, seen)

        def stateful(argv):
            if argv == ["iam", "generate-credential-report"]:
                return main.CallResult(0, json.dumps({"State": next(states)}), "")
            return runner(argv)
        bundle = main.collect(stateful, "us-east-1", clock=lambda: NOW, sleep=sleeps.append)
        for key in ("s3control get-public-access-block --account-id 123456789012", "s3api get-bucket-acl --bucket b1", "cloudtrail get-trail-status --name t1",
                    "cloudtrail get-trail-status --name arn:t2", "iam get-access-key-last-used --access-key-id AKIA1", "kms get-key-rotation-status --key-id k1", "iam get-credential-report"):
            self.assertIn(key, bundle["calls"])
        self.assertNotIn("kms get-key-rotation-status --key-id k2", bundle["calls"])
        self.assertEqual(bundle["calls"]["s3api get-public-access-block --bucket b1"]["error"], "NoSuchPublicAccessBlockConfiguration")
        self.assertEqual(sleeps, [1, 1])
        self.assertEqual((bundle["account"], bundle["collected_at"]), (ACCOUNT, "2026-11-01T12:00:00+00:00"))

    def test_aws_cli_runner_adds_output_region_and_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "aws")
            with open(fake, "w") as handle:
                handle.write("#!/bin/sh\nprintf '{\"argv\": \"%s\", \"pager\": \"%s\"}' \"$*\" \"$AWS_PAGER\"\n")
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
            result = main.aws_cli("http://localhost:5000", "eu-central-1", fake)(["ec2", "describe-vpcs"])
            self.assertEqual(json.loads(result.stdout), {"argv": "ec2 describe-vpcs --output json --region eu-central-1 --endpoint-url http://localhost:5000", "pager": ""})
            with self.assertRaises(main.PostureError):
                main.aws_cli(binary=os.path.join(tmp, "missing"))(["sts", "get-caller-identity"])


class CliTests(unittest.TestCase):
    def test_bad_input_exits_2_with_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "not-a-bundle.json")
            with open(path, "w") as handle:
                json.dump({"Buckets": []}, handle)
            code, _, errors = cli("scan", path)
            self.assertEqual(code, 2)
            self.assertIn("not a posture bundle", errors)
            self.assertEqual(cli("scan", os.path.join(tmp, "missing.json"))[0], 2)
            self.assertIn("--now", cli("scan", FIXTURE, "--now", "soon")[2])

    def test_json_commands_and_markdown_table(self):
        code, out, _ = cli("scan", FIXTURE, "--json")
        doc = json.loads(out)
        nacl_fix = next(f for f in doc["findings"] if f["check"] == "nacl-admin-open" and f["name"] == "cc-wide-range")
        self.assertEqual([c[c.index("--rule-number") + 1] for c in nacl_fix["commands"]], ["99", "98"])
        self.assertEqual(doc["benchmark"], "CIS AWS Foundations Benchmark v5.0.0")
        code, out, _ = cli("scan", FIXTURE, "--markdown")
        self.assertTrue(out.startswith("| Severity | Check | Resource | CIS v5.0.0 | Security Hub | Evidence |"))
        code, out, _ = cli("checks", "--json")
        self.assertEqual(len(json.loads(out)), len(main.CHECKS))


if __name__ == "__main__":
    unittest.main()
