import contextlib
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import main  # noqa: E402

FIXTURES = os.path.join(HERE, "..", "fixtures")
REPOS = os.path.join(FIXTURES, "repos")
ROOT_LOCATION = os.path.join(REPOS, "catalog", "all.yaml")


def fixture_json(*parts):
    with open(os.path.join(FIXTURES, *parts)) as handle:
        return json.load(handle)


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def write(directory, name, text):
    path = os.path.join(directory, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(textwrap.dedent(text))
    return path


class YamlSubsetTests(unittest.TestCase):
    def test_parses_the_shapes_catalog_files_use(self):
        text = textwrap.dedent("""\
            # leading comment
            apiVersion: backstage.io/v1alpha1
            kind: Component
            metadata:
              name: demo   # trailing comment
              description: >
                folded across
                two lines
              annotations:
                example.com/quoted: "a # not a comment"
                example.com/single: 'it''s'
                example.com/number: 3
              links:
                - url: https://example.com/runbook
                  title: Runbook
            spec:
              tags: [a, "b, c", 7]
              empty: {}
              list:
              - one
              - two
              definition: |
                line one
                  indented
              nothing:
            ---
            ---
            - just
            - a list
            """)
        docs = main.parse_yaml_documents(text)
        self.assertEqual([index for index, _, _ in docs], [0, 1])
        first = docs[0][1]
        self.assertEqual(first["metadata"]["name"], "demo")
        self.assertEqual(first["metadata"]["description"], "folded across two lines\n")
        self.assertEqual(first["metadata"]["annotations"], {"example.com/quoted": "a # not a comment", "example.com/single": "it's", "example.com/number": 3})
        self.assertEqual(first["metadata"]["links"], [{"url": "https://example.com/runbook", "title": "Runbook"}])
        self.assertEqual(first["spec"]["tags"], ["a", "b, c", 7])
        self.assertEqual(first["spec"]["empty"], {})
        self.assertEqual(first["spec"]["list"], ["one", "two"])
        self.assertEqual(first["spec"]["definition"], "line one\n  indented\n")
        self.assertIsNone(first["spec"]["nothing"])
        self.assertEqual(docs[1][1], ["just", "a list"])
        self.assertEqual(docs[1][2], 28)

    def test_errors_name_the_line(self):
        with self.assertRaises(main.YamlError) as caught:
            main.parse_yaml_documents("a: 1\nb: 2\na: 3\n", "x.yaml")
        self.assertEqual(caught.exception.line, 3)
        self.assertIn("duplicate key", str(caught.exception))
        with self.assertRaises(main.YamlError) as caught:
            main.parse_yaml_documents("a:\n\tb: 1\n", "x.yaml")
        self.assertIn("tabs", str(caught.exception))
        with self.assertRaises(main.YamlError):
            main.parse_yaml_documents("a: &anchor 1\n", "x.yaml")


class ReferenceTests(unittest.TestCase):
    def test_defaults_fill_only_what_is_missing(self):
        self.assertEqual(main.parse_ref("payments-team", "Group", "payments"), ("Group", "payments", "payments-team"))
        self.assertEqual(main.parse_ref("resource:db", None, "default"), ("resource", "default", "db"))
        self.assertEqual(main.parse_ref("team-a/api", "API", "default"), ("API", "team-a", "api"))
        self.assertEqual(main.parse_ref("component:ns/x:y", None), ("component", "ns", "x:y"))

    def test_unparseable_references_are_errors(self):
        for text, default in (("payments-db", None), ("group:", "Group"), ("system:payments/", "System"), (":x", "Group")):
            with self.assertRaises(main.RefError, msg=text):
                main.parse_ref(text, default)


class ValidationTests(unittest.TestCase):
    def test_root_location_pulls_in_every_repository(self):
        loaded = main.load([ROOT_LOCATION])
        self.assertEqual(len(loaded.files), 8)
        self.assertEqual(len(loaded.entities), 28)
        self.assertEqual([e.ref for e in loaded.entities if not e.accepted], [])
        self.assertEqual(loaded.errors, [])
        self.assertEqual([s["path"] for s in loaded.skipped], ["fraud-ml/catalog-info.yaml"])

    def test_each_broken_entity_is_rejected_for_exactly_its_planted_field(self):
        expected = fixture_json("expected", "broken-fields.json")
        loaded = main.load([os.path.join(FIXTURES, "broken")])
        found = {f"{e.source}#{e.doc}": [f.field for f in e.findings if f.severity == "error"] for e in loaded.entities}
        self.assertEqual(found, {key: [value] for key, value in expected.items()})

    def test_kind_and_version_checks(self):
        findings = main.validate_entity({"apiVersion": "backstage.io/v1beta1", "kind": "Resource", "metadata": {"name": "db"}, "spec": {"type": "database", "owner": "team"}})
        self.assertEqual(findings, [])
        findings = main.validate_entity({"apiVersion": "backstage.io/v1alpha1", "kind": "Template", "metadata": {"name": "t"}})
        self.assertEqual([f.field for f in findings], ["kind"])

    def test_missing_required_location_target_fails_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "root.yaml", """\
                apiVersion: backstage.io/v1alpha1
                kind: Location
                metadata:
                  name: root
                spec:
                  targets: [./gone/catalog-info.yaml]
                """)
            code, out, _ = run_cli("validate", os.path.join(tmp, "root.yaml"), "--json")
            self.assertEqual(code, 1)
            report = json.loads(out)
            self.assertEqual([e["path"] for e in report["load_errors"]], ["gone/catalog-info.yaml"])

    def test_duplicate_entity_is_a_conflict_and_the_first_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = """\
                apiVersion: backstage.io/v1alpha1
                kind: Resource
                metadata:
                  name: shared-db
                spec: {type: database, owner: %s}
                """
            write(tmp, "a/catalog-info.yaml", body % "team-a")
            write(tmp, "b/catalog-info.yaml", body % "team-b")
            catalog = main.load([tmp]).catalog
            self.assertEqual(catalog.owners("resource:default/shared-db"), ["group:default/team-a"])
            self.assertEqual([c["source"] for c in catalog.conflicts], ["b/catalog-info.yaml"])


class RelationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loaded = main.load([REPOS])
        cls.catalog = cls.loaded.catalog
        cls.relations = set(cls.catalog.relations())

    def test_every_relation_has_its_reverse(self):
        for pair in (("component:default/ledger", "ownedBy", "group:default/payments-team"),
                     ("group:default/payments-team", "ownerOf", "component:default/ledger"),
                     ("component:default/payments-api", "dependsOn", "component:default/ledger"),
                     ("component:default/ledger", "dependencyOf", "component:default/payments-api"),
                     ("component:default/payments-api", "providesApi", "api:default/payments-api"),
                     ("api:default/payments-api", "apiProvidedBy", "component:default/payments-api"),
                     ("api:default/search-api", "apiConsumedBy", "component:default/checkout-web"),
                     ("system:default/payments", "partOf", "domain:default/commerce"),
                     ("domain:default/commerce", "hasPart", "system:default/payments"),
                     ("group:default/engineering", "parentOf", "group:default/search-team"),
                     ("group:default/search-team", "childOf", "group:default/engineering")):
            self.assertIn(pair, self.relations)

    def test_membership_declared_on_either_side_appears_on_both(self):
        self.assertIn(("user:default/alice", "memberOf", "group:default/checkout-team"), self.relations)
        self.assertIn(("group:default/payments-team", "hasMember", "user:default/bob"), self.relations)

    def test_references_inherit_the_entity_namespace(self):
        broken = {(b["ref"], b["target"]) for b in self.catalog.broken_references()}
        self.assertEqual(broken, {("component:payments/receipts-worker", "group:payments/payments-team"),
                                  ("component:default/notifications", "group:default/comms-team")})
        self.assertIn(("component:payments/receipts-worker", "consumesApi", "api:default/payments-api"), self.relations)

    def test_owner_of_a_kind_that_cannot_own_is_dropped(self):
        self.assertEqual(self.catalog.owners("component:default/fx-rates"), [])
        self.assertEqual([(d["ref"], d["field"]) for d in self.catalog.dropped], [("component:default/fx-rates", "spec.owner")])

    def test_reverse_relation_only_for_declared_kind_pairs(self):
        entity = main.Entity({"apiVersion": "backstage.io/v1alpha1", "kind": "Component", "metadata": {"name": "c"},
                              "spec": {"type": "service", "lifecycle": "production", "owner": "user:jo", "dependsOn": ["api:x"]}}, "x", 0, 1)
        edges, _ = main.edges_of(entity)
        triples = {(e.source, e.relation, e.target) for e in edges}
        self.assertIn(("component:default/c", "dependsOn", "api:default/x"), triples)
        self.assertNotIn(("api:default/x", "dependencyOf", "component:default/c"), triples)
        self.assertIn(("user:default/jo", "ownerOf", "component:default/c"), triples)


class QueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loaded = main.load([REPOS])
        cls.catalog = cls.loaded.catalog

    def test_blast_radius_matches_the_expected_fixture(self):
        report = main.blast_radius(self.catalog, self.catalog.resolve("payments-db"))
        self.assertEqual(report, fixture_json("expected", "blast-radius-payments-db.json"))

    def test_blast_radius_depth_limit(self):
        report = main.blast_radius(self.catalog, "resource:default/payments-db", max_depth=1)
        self.assertEqual([i["ref"] for i in report["impacted"]], ["component:default/ledger", "component:default/payments-api"])

    def test_resolve_needs_the_kind_only_when_ambiguous(self):
        self.assertEqual(self.catalog.resolve("ledger"), "component:default/ledger")
        with self.assertRaises(main.InputError) as caught:
            self.catalog.resolve("payments-api")
        self.assertIn("ambiguous", str(caught.exception))
        with self.assertRaises(main.InputError):
            self.catalog.resolve("component:nope")

    def test_owner_report_walks_members_escalation_and_on_call(self):
        report = main.owner_report(self.catalog, "component:default/ledger")
        owner = report["owners"][0]
        self.assertEqual(owner["members"], ["user:default/bob", "user:default/carol"])
        self.assertEqual(owner["parents"], ["group:default/engineering"])
        self.assertEqual((report["oncall"], report["oncall_from"]), ({"opsgenie.com/team": "payments"}, "group:default/payments-team"))

    def test_gaps_match_the_expected_fixture_and_filter_by_team(self):
        self.assertEqual({"teams": main.gaps(self.catalog)}, fixture_json("expected", "gaps.json"))
        self.assertEqual(list(main.gaps(self.catalog, "group:default/comms-team")), ["group:default/comms-team"])

    def test_scorecard_matches_the_expected_fixture(self):
        report = main.scorecard(self.catalog, fixture_json("scorecard.json"))
        self.assertEqual(report, fixture_json("expected", "scorecard.json"))

    def test_scorecard_rejects_unknown_checks(self):
        card = {"levels": ["bronze"], "rules": [{"id": "x", "level": "bronze", "check": "vibes"}]}
        with self.assertRaises(main.InputError):
            main.scorecard(self.catalog, card)

    def test_orphans_from_a_previous_export(self):
        found = main.orphans(self.loaded, fixture_json("previous.json"))
        self.assertEqual([(o["ref"], o["status"]) for o in found],
                         [("component:default/legacy-billing", "orphan"), ("component:default/old-reporting", "orphan")])
        self.assertEqual(main.orphans(self.loaded, main.export(self.loaded)), [])

    def test_kubernetes_workloads_map_to_owners(self):
        report = main.k8s_owners(self.catalog, fixture_json("k8s", "deployments.json"))
        statuses = {w["object"].split("/")[-1]: w["status"] for w in report["workloads"]}
        self.assertEqual(statuses, {"checkout-web": "owned", "payments-api": "owned", "notifications": "no-owner",
                                    "search-v1": "no-component", "debug-shell": "unlabelled"})
        self.assertEqual(report["not_running"], ["component:default/search-api", "component:payments/receipts-worker"])


class CliTests(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(run_cli("validate", REPOS)[0], 0)
        self.assertEqual(run_cli("validate", os.path.join(FIXTURES, "broken"))[0], 1)
        self.assertEqual(run_cli("validate", os.path.join(FIXTURES, "nope"))[0], 2)
        self.assertEqual(run_cli("owner", "component:nope", REPOS)[0], 2)
        self.assertEqual(run_cli("gaps", REPOS)[0], 1)
        self.assertEqual(run_cli("lint", REPOS)[0], 1)
        rules = os.path.join(FIXTURES, "scorecard.json")
        self.assertEqual(run_cli("scorecard", REPOS, "--rules", rules)[0], 0)
        self.assertEqual(run_cli("scorecard", REPOS, "--rules", rules, "--min-level", "bronze")[0], 1)
        self.assertEqual(run_cli("scorecard", REPOS, "--rules", rules, "--min-level", "platinum")[0], 2)
        self.assertEqual(run_cli("--help")[0], 0)

    def test_json_output_is_parseable_and_diagnostics_go_to_stderr(self):
        code, out, err = run_cli("validate", REPOS, "--json")
        report = json.loads(out)
        self.assertEqual(report["summary"], {"files": 8, "entities": 28, "accepted": 28, "rejected": 0})
        code, out, err = run_cli("relations", REPOS, "--json", "--entity", "ledger")
        self.assertEqual(code, 0)
        self.assertEqual({r["type"] for r in json.loads(out)["relations"]}, {"ownedBy", "partOf", "dependsOn", "dependencyOf"})
        self.assertEqual(err, "")

    def test_yaml_error_is_reported_with_file_and_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "catalog-info.yaml", "apiVersion: backstage.io/v1alpha1\nkind: Component\nkind: API\n")
            code, out, _ = run_cli("validate", tmp, "--json")
            self.assertEqual(code, 1)
            self.assertIn("line 3", json.loads(out)["load_errors"][0]["message"])


if __name__ == "__main__":
    unittest.main()
