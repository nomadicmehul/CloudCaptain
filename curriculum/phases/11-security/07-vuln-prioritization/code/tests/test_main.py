import copy
import datetime as dt
import gzip
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
TRIVY_OLD = os.path.join(FIXTURES, "trivy-api-1.4.0.json")
TRIVY_NEW = os.path.join(FIXTURES, "trivy-api-1.5.0.json")
GRYPE_OLD = os.path.join(FIXTURES, "grype-api-1.4.0.json")
KEV = os.path.join(FIXTURES, "kev-2026-10-30.json")
EPSS = os.path.join(FIXTURES, "epss-2026-11-01.csv")
VEX = os.path.join(FIXTURES, "vex-api.openvex.json")
FEEDS = ["--kev", KEV, "--epss", EPSS, "--now", "2026-11-02"]


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


def run_json(argv):
    code, out, err = run(argv + ["--json"])
    return code, json.loads(out) if out.strip() else None, err


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write(directory, name, doc):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(doc if isinstance(doc, str) else json.dumps(doc))
    return path


def rows(findings):
    return [(f["vuln"], f["package"], f["priority"]) for f in findings]


def context(**overrides):
    values = dict(policy=main.load_policy(main.DEFAULT_POLICY), exposure="internet", now=dt.date(2026, 11, 2),
                  kev=main.load_kev(KEV), epss=main.load_epss(EPSS))
    values.update(overrides)
    return main.Context(**values)


class CvssTests(unittest.TestCase):
    def test_v3_base_scores_match_every_first_example(self):
        examples = load(os.path.join(FIXTURES, "cvss-examples.json"))["v3.1"]
        self.assertGreaterEqual(len(examples), 40)
        for vector, expected, label in examples:
            with self.subTest(label=label):
                self.assertEqual(main.score_vector(vector).score, expected)

    def test_v4_scores_match_every_first_example_and_the_rounding_edge_cases(self):
        doc = load(os.path.join(FIXTURES, "cvss-examples.json"))
        self.assertGreaterEqual(len(doc["v4.0"]), 40)
        for vector, expected, label in doc["v4.0"] + doc["rounding"]:
            with self.subTest(vector=vector, label=label):
                self.assertEqual(main.score_vector(vector).score, expected)

    def test_v4_lookup_table_has_one_score_per_reachable_macrovector(self):
        self.assertEqual(len(main.V4_MACRO_SCORES.split()), 270)
        self.assertEqual(len(main.V4_LOOKUP), 270)
        self.assertEqual(main.V4_LOOKUP["000000"], 10.0)
        self.assertEqual(main.V4_LOOKUP["212221"], 0.1)
        self.assertNotIn("002000", main.V4_LOOKUP)

    def test_v4_threat_metric_defaults_to_attacked_and_lowers_the_score_when_unreported(self):
        base = "CVSS:4.0/AV:P/AC:H/AT:P/PR:H/UI:A/VC:H/VI:H/VA:H/SC:H/SI:H/SA:H"
        self.assertEqual(main.score_vector(base).score, 7.3)
        self.assertEqual(main.score_vector(base + "/E:A").score, 7.3)
        self.assertEqual(main.score_vector(base + "/E:P").score, 5.9)
        self.assertEqual(main.score_vector(base + "/E:U").score, 4.1)
        self.assertEqual(main.score_vector(base).nomenclature, "CVSS-B")
        self.assertEqual(main.score_vector(base + "/E:U").nomenclature, "CVSS-BT")
        self.assertEqual(main.score_vector(base + "/E:U/CR:L").nomenclature, "CVSS-BTE")
        network = main.score_vector("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N")
        self.assertEqual((network.score, network.severity, network.macrovector), (9.3, "CRITICAL", "000200"))

    def test_scope_cap_no_impact_and_roundup(self):
        self.assertEqual(main.score_vector("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H").score, 10.0)
        self.assertEqual(main.score_vector("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N").score, 0.0)
        self.assertEqual(main.score_vector("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N").severity, "NONE")
        self.assertEqual(main.score_vector("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:N/VI:N/VA:N/SC:N/SI:N/SA:N/E:A").score, 0.0)
        self.assertEqual(main.score_vector("CVSS:3.0/AV:L/AC:L/PR:L/UI:N/S:C/C:N/I:H/A:N").score, 6.5)
        self.assertEqual(main.roundup(4.02), 4.1)
        self.assertEqual(main.roundup(4.000000000000001), 4.0)
        self.assertEqual(main.roundup(4.0), 4.0)

    def test_malformed_vectors_are_input_errors_with_the_reason(self):
        cases = {
            "AV:N/AC:L/Au:N/C:P/I:P/A:P": "CVSS v2",
            "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H": "missing base metric(s) A",
            "CVSS:3.1/AV:N/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H": "appears twice",
            "CVSS:3.1/AV:X/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H": "AV:X is not one of",
            "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N/ZZ:1": "unknown metric",
        }
        for vector, message in cases.items():
            with self.subTest(vector=vector):
                with self.assertRaises(main.InputError) as caught:
                    main.score_vector(vector)
                self.assertIn(message, str(caught.exception))

    def test_cvss_command_reads_published_test_vector_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, "vectors", "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H - (9.8, 9.8, 9.8)\n\nCVSS:4.0/AV:L/AC:L/AT:N/PR:N/UI:N/VC:N/VI:N/VA:H/SC:N/SI:N/SA:N - 6.9\n")
            code, doc, _ = run_json(["cvss", "--file", path])
            self.assertEqual(code, 0)
            self.assertEqual([(d["version"], d["score"]) for d in doc], [("3.1", 9.8), ("4.0", 6.9)])
            code, out, _ = run(["cvss", "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", "CVSS:3.1/AV:N"])
            self.assertEqual(code, 1)
            self.assertIn("9.8  CRITICAL", out)
            self.assertIn("error  CVSS:3.1/AV:N: missing base metric", out)
            code, _, err = run(["cvss"])
            self.assertEqual(code, 2)
            self.assertIn("at least one vector", err)


class PurlTests(unittest.TestCase):
    def test_purls_decode_before_comparison_and_match_like_go_vex(self):
        trivy = "pkg:deb/debian/login@1:4.13%2Bdfsg1-1%2Bb1?arch=amd64&distro=debian-12.1"
        grype = "pkg:deb/debian/login@1%3A4.13%2Bdfsg1-1%2Bb1?arch=amd64&distro=debian-12&upstream=shadow%401%3A4.13%2Bdfsg1-1"
        a, b = main.parse_purl(trivy), main.parse_purl(grype)
        self.assertEqual((a.type, a.namespace, a.name, a.version), ("deb", "debian", "login", "1:4.13+dfsg1-1+b1"))
        self.assertEqual(a.version, b.version)
        self.assertEqual(b.qualifiers["upstream"], "shadow@1:4.13+dfsg1-1")
        self.assertTrue(main.purl_matches("pkg:deb/debian/login@1:4.13+dfsg1-1+b1", trivy))
        self.assertTrue(main.purl_matches("pkg:deb/debian/login@1:4.13+dfsg1-1+b1", grype))
        self.assertTrue(main.purl_matches("pkg:deb/debian/login", grype))
        self.assertFalse(main.purl_matches("pkg:deb/debian/login@1:4.13+dfsg1-1", grype))
        self.assertFalse(main.purl_matches("pkg:deb/debian/login?distro=debian-13", grype))
        self.assertFalse(main.purl_matches(grype, "pkg:deb/debian/login@1:4.13+dfsg1-1+b1"))
        scoped = main.parse_purl("pkg:npm/%40babel/traverse@7.23.1")
        self.assertEqual(main.package_identity("npm", scoped, "x"), "@babel/traverse")
        self.assertEqual(main.package_identity("maven", main.parse_purl("pkg:maven/org.apache.logging.log4j/log4j-core@2.14.1"), "log4j-core"), "org.apache.logging.log4j:log4j-core")
        self.assertEqual(main.package_identity("pypi", None, "Foo_Bar.baz"), "foo-bar-baz")


class IngestTests(unittest.TestCase):
    def test_trivy_report_becomes_normalised_findings(self):
        report = main.load_report(TRIVY_OLD)
        self.assertEqual((report.tool, report.artifact, report.created, len(report.findings)), ("trivy", "registry.example.com/shop/api:1.4.0", "2026-10-01", 12))
        zlib = next(f for f in report.findings if f.package == "zlib1g")
        self.assertEqual((zlib.ecosystem, zlib.version, zlib.state, zlib.fixed), ("deb", "1:1.2.13.dfsg-1", "wont-fix", set()))
        glibc = next(f for f in report.findings if f.package == "libc6" and f.vuln == "CVE-2023-4911")
        self.assertEqual(glibc.fixed, {"2.36-9+deb12u3"})
        self.assertEqual((glibc.aliases, glibc.advisories), ([], {"DSA-5514-1"}))
        self.assertEqual({c.source for c in glibc.claims}, {"nvd", "redhat"})
        idna = next(f for f in report.findings if f.package == "idna")
        self.assertEqual({c.vector[:8] for c in idna.claims}, {"CVSS:3.1", "CVSS:4.0"})
        self.assertIn("pkg:oci/api@sha256:c2ca5cb8b2fdd4b89680229b222e06de684bcedbecb33f212fe42a954a6ba6eb", report.identities)

    def test_grype_report_maps_ghsa_to_its_cve_and_keeps_upstream(self):
        report = main.load_report(GRYPE_OLD)
        self.assertEqual((report.tool, report.created, len(report.findings)), ("grype", "2026-10-01", 12))
        idna = next(f for f in report.findings if f.package == "idna")
        self.assertEqual((idna.vuln, idna.aliases, idna.ecosystem), ("CVE-2024-3651", ["GHSA-jjg7-2v4v-x38h"], "pypi"))
        login = next(f for f in report.findings if f.package == "login")
        self.assertEqual((login.upstream, login.version), ("shadow", "1:4.13+dfsg1-1+b1"))
        self.assertEqual(next(f for f in report.findings if f.package == "coreutils").state, "wont-fix")
        self.assertTrue(any(c.source == "cisa-adp" for f in report.findings for c in f.claims))

    def test_merge_deduplicates_the_overlap_between_tools(self):
        code, doc, _ = run_json(["ingest", TRIVY_OLD, GRYPE_OLD])
        self.assertEqual(code, 0)
        findings = doc["findings"]
        self.assertEqual(len(findings), 13)
        tools = {(f["vuln"], f["package"]): tuple(f["tools"]) for f in findings}
        self.assertEqual(sum(1 for t in tools.values() if t == ("grype", "trivy")), 11)
        self.assertEqual(tools[("CVE-2011-3374", "apt")], ("trivy",))
        self.assertEqual(tools[("CVE-2024-28085", "mount")], ("grype",))
        idna = next(f for f in findings if f["package"] == "idna")
        self.assertEqual((idna["vuln"], idna["aliases"]), ("CVE-2024-3651", ["GHSA-jjg7-2v4v-x38h"]))
        self.assertEqual(len(idna["purls"]), 1)
        login = next(f for f in findings if f["package"] == "login")
        self.assertEqual(len(login["purls"]), 2)
        for f in findings:
            for claim in f["cvss"]:
                with self.subTest(vuln=f["vuln"], vector=claim["vector"]):
                    self.assertEqual(claim["computed"], claim["reported"])

    def test_alias_merge_joins_a_ghsa_only_finding_to_the_same_advisory_with_a_cve(self):
        trivy = {"SchemaVersion": 2, "ArtifactName": "app", "Results": [{"Target": "Python", "Type": "python-pkg", "Vulnerabilities": [
            {"VulnerabilityID": "GHSA-aaaa-bbbb-cccc", "PkgName": "Foo_Bar", "InstalledVersion": "1.0", "Severity": "HIGH"}]}]}
        grype = {"descriptor": {"name": "grype"}, "source": {"target": "app"}, "matches": [
            {"vulnerability": {"id": "GHSA-aaaa-bbbb-cccc", "severity": "High", "fix": {"versions": ["1.1"], "state": "fixed"}},
             "relatedVulnerabilities": [{"id": "CVE-2026-0001", "cvss": [{"source": "nvd@nist.gov", "vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", "metrics": {"baseScore": 9.8}}]}],
             "artifact": {"name": "foo-bar", "version": "1.0", "type": "python", "purl": "pkg:pypi/foo-bar@1.0"}}]}
        with tempfile.TemporaryDirectory() as tmp:
            code, doc, _ = run_json(["ingest", write(tmp, "t.json", trivy), write(tmp, "g.json", grype)])
        self.assertEqual(code, 0)
        self.assertEqual(len(doc["findings"]), 1)
        merged = doc["findings"][0]
        self.assertEqual((merged["vuln"], merged["aliases"], merged["package"], merged["fixed"], merged["tools"]),
                         ("CVE-2026-0001", ["GHSA-aaaa-bbbb-cccc"], "foo-bar", ["1.1"], ["grype", "trivy"]))

    def test_a_vendor_advisory_that_fixes_several_cves_does_not_merge_them(self):
        doc = load(TRIVY_OLD)
        first = next(v for v in doc["Results"][0]["Vulnerabilities"] if v["PkgName"] == "libc6" and v["VulnerabilityID"] == "CVE-2023-4911")
        sibling = copy.deepcopy(first)
        sibling["VulnerabilityID"] = "CVE-2023-4806"
        doc["Results"][0]["Vulnerabilities"].append(sibling)
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run_json(["ingest", write(tmp, "t.json", doc)])
        self.assertEqual(code, 0)
        libc6 = sorted(f["vuln"] for f in out["findings"] if f["package"] == "libc6")
        self.assertEqual(libc6, ["CVE-2023-4806", "CVE-2023-4911", "CVE-2024-2961"])
        self.assertTrue(all(f["advisories"] == ["DSA-5514-1"] for f in out["findings"] if f["vuln"] in ("CVE-2023-4806", "CVE-2023-4911")))

    def test_ingest_flags_a_reported_score_that_disagrees_with_its_vector(self):
        doc = load(TRIVY_OLD)
        doc["Results"][0]["Vulnerabilities"][0]["CVSS"]["nvd"]["V3Score"] = 9.1
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run(["ingest", write(tmp, "t.json", doc)])
        self.assertEqual(code, 0)
        self.assertIn("MISMATCH trivy/nvd reported 9.1, computed 7.8", out)

    def test_unknown_reports_and_unreadable_files_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = run(["ingest", write(tmp, "x.json", {"bomFormat": "CycloneDX"})])
            self.assertEqual(code, 2)
            self.assertIn("neither a Trivy JSON report", err)
            code, _, err = run(["ingest", os.path.join(tmp, "missing.json")])
            self.assertEqual(code, 2)
            self.assertIn("missing.json", err)


class FeedTests(unittest.TestCase):
    def test_kev_and_epss_feeds_load_with_their_dates(self):
        kev = main.load_kev(KEV)
        self.assertEqual((kev.version, len(kev.entries)), ("2026.10.30", 7))
        self.assertEqual(kev.entries["CVE-2023-4911"].added, "2023-11-21")
        self.assertEqual(kev.entries["CVE-2021-44228"].ransomware, "Known")
        epss = main.load_epss(EPSS)
        self.assertEqual((epss.model, epss.date[:10], len(epss.scores)), ("v2026.06.15", "2026-11-01", 16))
        self.assertEqual(epss.scores["CVE-2024-2961"].probability, 0.8833)
        with tempfile.TemporaryDirectory() as tmp:
            gz = os.path.join(tmp, "epss.csv.gz")
            with open(EPSS, "rb") as source, gzip.open(gz, "wb") as target:
                target.write(source.read())
            self.assertEqual(main.load_epss(gz).scores, epss.scores)
            api = write(tmp, "api.json", {"status": "OK", "data": [{"cve": "CVE-2023-4911", "epss": "0.637690000", "percentile": "0.992210000", "date": "2026-11-01"}]})
            self.assertEqual(main.load_epss(api).scores["CVE-2023-4911"].probability, 0.63769)
            bad = write(tmp, "bad.csv", "cve,epss,percentile\nCVE-2023-4911,1.7,0.9\n")
            with self.assertRaises(main.InputError):
                main.load_epss(bad)
            with self.assertRaises(main.InputError):
                main.load_kev(write(tmp, "notkev.json", {"items": []}))


class VexTests(unittest.TestCase):
    def test_package_products_and_image_subcomponents_suppress_the_same_findings(self):
        code, doc, _ = run_json(["rank", TRIVY_OLD, GRYPE_OLD, "--vex", VEX, "--exposure", "internet"] + FEEDS)
        self.assertEqual(code, 0)
        suppressed = {(f["vuln"], f["package"], f["vex"]["justification"]) for f in doc["suppressed"]}
        self.assertEqual(suppressed, {("CVE-2023-45853", "zlib1g", "vulnerable_code_not_present"), ("CVE-2024-3651", "idna", "vulnerable_code_not_in_execute_path")})
        investigating = [f for f in doc["findings"] if f["vex"]]
        self.assertEqual({(f["package"], f["vex"]["status"]) for f in investigating}, {("util-linux", "under_investigation"), ("mount", "under_investigation")})
        self.assertEqual(doc["summary"]["suppressed"], 2)

    def test_an_image_scoped_statement_does_not_apply_to_another_image(self):
        statements, problems = main.load_vex(VEX)
        self.assertEqual(problems, [])
        idna = next(f for f in main.load_report(TRIVY_OLD).findings if f.package == "idna")
        self.assertIsNotNone(main.vex_for(idna, statements, {"pkg:oci/api@sha256:c2ca5cb8b2fdd4b89680229b222e06de684bcedbecb33f212fe42a954a6ba6eb"}))
        self.assertIsNone(main.vex_for(idna, statements, {"pkg:oci/worker@sha256:0000"}))

    def test_the_latest_statement_wins(self):
        doc = load(VEX)
        newer = copy.deepcopy(doc["statements"][0])
        newer.update({"status": "affected", "action_statement": "rebuild on debian:12.14-slim", "timestamp": "2026-10-28T10:00:00Z"})
        newer.pop("justification")
        newer.pop("impact_statement")
        doc["statements"].append(newer)
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run_json(["rank", TRIVY_OLD, "--vex", write(tmp, "vex.json", doc)] + FEEDS)
        self.assertEqual(code, 0)
        zlib = next(f for f in out["findings"] if f["package"] == "zlib1g")
        self.assertEqual((zlib["vex"]["status"], zlib["vex"]["action_statement"]), ("affected", "rebuild on debian:12.14-slim"))

    def test_invalid_statements_are_listed_and_rank_refuses_them(self):
        doc = load(VEX)
        doc["statements"][0].pop("justification")
        doc["statements"][0].pop("impact_statement")
        doc["statements"][1]["justification"] = "not_reachable"
        doc["statements"].append({"vulnerability": {"name": "CVE-2011-3374"}, "products": [{"@id": "pkg:deb/debian/apt"}], "status": "affected"})
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, "vex.json", doc)
            code, out, _ = run(["vex", path])
            self.assertEqual(code, 1)
            self.assertIn("not_affected needs a justification or an impact_statement", out)
            self.assertIn("'not_reachable' is not one of", out)
            self.assertIn("affected needs an action_statement", out)
            code, _, err = run(["rank", TRIVY_OLD, "--vex", path] + FEEDS)
            self.assertEqual(code, 2)
            self.assertIn("refusing to suppress anything", err)


class RankTests(unittest.TestCase):
    def test_kev_first_then_epss_times_exposure_then_cvss(self):
        code, doc, _ = run_json(["rank", TRIVY_OLD, GRYPE_OLD, "--exposure", "internet"] + FEEDS)
        self.assertEqual(code, 0)
        order = rows(doc["findings"])
        self.assertEqual(order[:4], [("CVE-2023-4911", "libc-bin", "P1"), ("CVE-2023-4911", "libc6", "P1"), ("CVE-2024-2961", "libc-bin", "P2"), ("CVE-2024-2961", "libc6", "P2")])
        self.assertEqual(order[4], ("CVE-2023-45853", "zlib1g", "P3"))
        by_cvss = sorted(doc["findings"], key=lambda f: -f["score"]["value"])
        self.assertEqual(by_cvss[0]["vuln"], "CVE-2023-45853")
        top = doc["findings"][0]
        self.assertEqual((top["kev"]["added"], top["epss"]["probability"], top["score"]["source"]), ("2023-11-21", 0.63769, "nvd"))
        self.assertEqual(doc["summary"], {"P1": 2, "P2": 2, "P3": 3, "P4": 6, "suppressed": 0, "overdue": 4})

    def test_exposure_moves_epss_driven_findings_but_never_kev(self):
        code, doc, _ = run_json(["rank", TRIVY_OLD, GRYPE_OLD, "--exposure", "isolated"] + FEEDS)
        self.assertEqual(code, 0)
        priority = {(f["vuln"], f["package"]): f["priority"] for f in doc["findings"]}
        self.assertEqual(priority[("CVE-2023-4911", "libc6")], "P1")
        self.assertEqual(priority[("CVE-2024-2961", "libc6")], "P3")
        threat = next(f["threat"] for f in doc["findings"] if f["vuln"] == "CVE-2024-2961")
        self.assertAlmostEqual(threat, 0.08833)
        code, _, err = run(["rank", TRIVY_OLD, "--exposure", "dmz"] + FEEDS)
        self.assertEqual(code, 2)
        self.assertIn("internet, internal, isolated", err)

    def test_due_dates_count_from_first_seen_and_report_overdue_days(self):
        ctx = context()
        findings = main.merge(main.load_report(TRIVY_OLD).findings)
        active, _ = main.rank(findings, ctx)
        top = active[0]
        self.assertEqual((top.priority, top.due, top.overdue_days), ("P1", "2026-10-08", 25))
        later = main.rank(findings, context(now=dt.date(2026, 10, 5)))[0][0]
        self.assertEqual(later.overdue_days, 0)
        unseen = main.Finding("CVE-2023-4911", {"CVE-2023-4911"}, "deb", "libc6", "2.36-9+deb12u1")
        self.assertEqual(main.assess(unseen, ctx).due, "2026-11-09")

    def test_missing_epss_and_missing_fix_are_explained(self):
        ctx = context()
        temp = main.Finding("TEMP-0841856-B18BAF", {"TEMP-0841856-B18BAF"}, "deb", "bash", "5.2.15-2+b2", state="not-fixed", first_seen="2026-10-01")
        reasons = " | ".join(main.assess(temp, ctx).reasons)
        self.assertIn("no EPSS score: not a CVE id", reasons)
        self.assertIn("no fixed version (not-fixed)", reasons)
        self.assertEqual(main.assess(temp, ctx).priority, "P4")

    def test_custom_policy_and_fail_on(self):
        policy = {"name": "strict", "exposure": {"prod": 1.0}, "tiers": [{"priority": "now", "kev": True, "sla_days": 2}, {"priority": "later", "sla_days": 60}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, "policy.json", policy)
            code, doc, _ = run_json(["rank", TRIVY_OLD, "--policy", path, "--exposure", "prod", "--fail-on", "now"] + FEEDS)
            self.assertEqual(code, 1)
            self.assertEqual((doc["policy"], doc["findings"][0]["priority"], doc["findings"][0]["due"]), ("strict", "now", "2026-10-03"))
            code, _, _ = run(["rank", TRIVY_NEW, "--policy", path, "--exposure", "prod", "--fail-on", "now"] + FEEDS)
            self.assertEqual(code, 0)
            policy["tiers"][-1]["min_cvss"] = 4.0
            code, _, err = run(["rank", TRIVY_OLD, "--policy", write(tmp, "bad.json", policy), "--exposure", "prod"] + FEEDS)
            self.assertEqual(code, 2)
            self.assertIn("last tier must have no conditions", err)
        code, _, err = run(["rank", TRIVY_OLD, "--fail-on", "P9"] + FEEDS)
        self.assertEqual(code, 2)
        self.assertIn("P1, P2, P3, P4", err)

    def test_text_output_explains_the_top_finding(self):
        code, out, _ = run(["rank", TRIVY_OLD, GRYPE_OLD, "--exposure", "internet", "--top", "3", "--vex", VEX] + FEEDS)
        self.assertEqual(code, 0)
        self.assertIn("P1 = in KEV, 7 d; P2 = EPSS x exposure >= 0.1, 30 d; P3 = CVSS >= 7, 90 d; P4 = the rest, 180 d", out)
        self.assertIn("why #1: KEV since 2023-11-21 (GNU C Library)", out)
        self.assertIn("... 8 more", out)
        self.assertIn("suppressed by VEX (2):", out)


class DiffTests(unittest.TestCase):
    def test_diff_splits_new_fixed_and_still_open_and_keeps_first_seen(self):
        code, doc, _ = run_json(["diff", "--old", TRIVY_OLD, "--new", TRIVY_NEW, "--exposure", "internet"] + FEEDS)
        self.assertEqual(code, 1)
        self.assertEqual(doc["warnings"], [])
        fixed = {(f["vuln"], f["package"]) for f in doc["fixed"]}
        self.assertEqual(len(fixed), 9)
        self.assertIn(("CVE-2023-4911", "libc6"), fixed)
        self.assertIn(("CVE-2024-3651", "idna"), fixed)
        self.assertEqual([(f["vuln"], f["package"], f["first_seen"]) for f in doc["new_findings"]], [("CVE-2024-47081", "requests", "2026-11-01")])
        still = {(f["vuln"], f["package"]): (f["first_seen"], f["due"]) for f in doc["still_open"]}
        self.assertEqual(still, {("CVE-2023-45853", "zlib1g"): ("2026-10-01", "2026-12-30"), ("CVE-2016-2781", "coreutils"): ("2026-10-01", "2027-03-30"),
                                 ("CVE-2011-3374", "apt"): ("2026-10-01", "2027-03-30")})
        code, _, _ = run(["diff", "--old", TRIVY_NEW, "--new", TRIVY_NEW] + FEEDS)
        self.assertEqual(code, 0)

    def test_a_new_finding_suppressed_by_vex_does_not_fail_the_diff(self):
        doc = {"@context": "https://openvex.dev/ns/v0.2.0", "@id": "https://example.com/vex/requests", "author": "security@example.com",
               "timestamp": "2026-11-01T00:00:00Z",
               "statements": [{"vulnerability": {"name": "CVE-2024-47081"}, "products": [{"@id": "pkg:pypi/requests@2.32.3"}],
                               "status": "not_affected", "justification": "vulnerable_code_not_in_execute_path"}]}
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run_json(["diff", "--old", TRIVY_OLD, "--new", TRIVY_NEW, "--vex", write(tmp, "vex.json", doc)] + FEEDS)
            text_code, text, _ = run(["diff", "--old", TRIVY_OLD, "--new", TRIVY_NEW, "--vex", os.path.join(tmp, "vex.json")] + FEEDS)
        self.assertEqual((code, text_code), (0, 0))
        self.assertEqual(out["new_findings"], [])
        self.assertEqual([(f["vuln"], f["package"], f["vex"]["status"]) for f in out["suppressed"]], [("CVE-2024-47081", "requests", "not_affected")])
        self.assertIn("suppressed by VEX (1):", text)

    def test_diff_warns_when_the_two_sides_used_different_tools(self):
        code, out, _ = run(["diff", "--old", TRIVY_OLD, GRYPE_OLD, "--new", TRIVY_NEW] + FEEDS)
        self.assertEqual(code, 1)
        self.assertIn("warning: the old side was scanned with grype, trivy and the new side with trivy", out)
        self.assertIn("- CVE-2024-28085    deb/mount", out)


if __name__ == "__main__":
    unittest.main()
