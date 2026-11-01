import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "rbac.json")
DASHBOARD = "system:serviceaccount:shop:dashboard"
CI = "system:serviceaccount:shop:ci"
G = "rbac.authorization.k8s.io"


def fixture_policy(**kwargs):
    with open(FIXTURE, encoding="utf-8") as handle:
        return main.load_policy([json.load(handle)], **kwargs)


def ask(policy, user, verb, target, namespace="shop", groups=(), subresource="", implicit=True):
    request, _ = main.parse_request(verb, target, namespace, subresource)
    return main.authorize(policy, main.identity_for(user, groups, implicit), request)


def cluster_role(name, rules, labels=None, selectors=None):
    doc = {"kind": "ClusterRole", "metadata": {"name": name, "labels": labels or {}}, "rules": rules}
    if selectors is not None:
        doc["aggregationRule"] = {"clusterRoleSelectors": selectors}
    return doc


def binding(kind, name, role_kind, role_name, subjects, namespace=None):
    meta = {"name": name}
    if namespace:
        meta["namespace"] = namespace
    return {"kind": kind, "metadata": meta, "roleRef": {"apiGroup": G, "kind": role_kind, "name": role_name}, "subjects": subjects}


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class IdentityTests(unittest.TestCase):
    def test_service_account_gets_the_groups_impersonation_adds(self):
        identity = main.identity_for(DASHBOARD)
        self.assertEqual(identity.groups, ["system:serviceaccounts", "system:serviceaccounts:shop", "system:authenticated"])

    def test_explicit_groups_replace_the_service_account_groups_but_keep_authenticated(self):
        self.assertEqual(main.identity_for(DASHBOARD, ["ops"]).groups, ["ops", "system:authenticated"])
        self.assertEqual(main.identity_for("jane", ["ops", "ops"]).groups, ["ops", "system:authenticated"])

    def test_anonymous_and_unauthenticated_do_not_get_authenticated(self):
        self.assertEqual(main.identity_for("system:anonymous").groups, [])
        self.assertEqual(main.identity_for("jane", ["system:unauthenticated"]).groups, ["system:unauthenticated"])

    def test_subject_access_review_mode_uses_exactly_the_groups_given(self):
        self.assertEqual(main.identity_for(DASHBOARD, implicit=False).groups, [])

    def test_malformed_service_account_names_are_plain_users(self):
        self.assertIsNone(main.split_service_account("system:serviceaccount:shop"))
        self.assertIsNone(main.split_service_account("system:serviceaccount:shop:a:b"))
        self.assertEqual(main.split_service_account(DASHBOARD), ("shop", "dashboard"))


class RequestTests(unittest.TestCase):
    def test_type_slash_name_is_a_resource_name_not_a_subresource(self):
        request, _ = main.parse_request("get", "pods/log", "shop")
        self.assertEqual((request.resource, request.name, request.subresource), ("pods", "log", ""))
        request, _ = main.parse_request("get", "pods/web-1", "shop", "log")
        self.assertEqual((request.combined_resource, request.name), ("pods/log", "web-1"))

    def test_groups_come_from_the_table_short_names_or_the_dotted_form(self):
        self.assertEqual(main.resolve_resource("deploy")[:2], ("deployments", "apps"))
        self.assertEqual(main.resolve_resource("Leases.coordination.k8s.io")[:2], ("leases", "coordination.k8s.io"))
        self.assertEqual(main.resolve_resource("networkpolicy")[:2], ("networkpolicies", "networking.k8s.io"))
        self.assertEqual(main.resolve_resource("*")[:2], ("*", ""))
        resource, group, warning = main.resolve_resource("widgets")
        self.assertEqual((resource, group), ("widgets", ""))
        self.assertIn("RESOURCE.GROUP", warning)

    def test_urls_are_non_resource_requests_and_reject_a_subresource(self):
        request, _ = main.parse_request("get", "/healthz", "shop")
        self.assertFalse(request.is_resource)
        with self.assertRaises(main.RbacError):
            main.parse_request("get", "/healthz", "", "log")

    def test_cluster_scoped_resource_with_a_namespace_warns(self):
        _, warnings = main.parse_request("list", "nodes", "shop")
        self.assertTrue(any("-A" in w for w in warnings))
        self.assertEqual(main.parse_request("list", "nodes", "")[1], [])


class RuleTests(unittest.TestCase):
    def request(self, verb, resource, group="", subresource="", name=""):
        return main.Request(verb, "shop", group, resource, subresource, name)

    def test_star_matches_subresources_but_a_plain_resource_does_not(self):
        everything = main.Rule(("*",), ("*",), ("*",))
        pods = main.Rule(("get",), ("",), ("pods",))
        self.assertTrue(main.rule_allows(everything, self.request("create", "pods", subresource="exec")))
        self.assertTrue(main.rule_allows(pods, self.request("get", "pods")))
        self.assertFalse(main.rule_allows(pods, self.request("get", "pods", subresource="log")))

    def test_star_slash_subresource_matches_that_subresource_of_any_resource(self):
        scale = main.Rule(("update",), ("apps",), ("*/scale",))
        self.assertTrue(main.rule_allows(scale, self.request("update", "deployments", "apps", "scale")))
        self.assertFalse(main.rule_allows(scale, self.request("update", "deployments", "apps")))
        self.assertFalse(main.rule_allows(scale, self.request("update", "deployments", "apps", "status")))

    def test_group_must_match_exactly(self):
        rule = main.Rule(("get",), ("",), ("deployments",))
        self.assertFalse(main.rule_allows(rule, self.request("get", "deployments", "apps")))

    def test_resource_names_never_match_a_request_without_a_name(self):
        rule = main.Rule(("get", "list"), ("",), ("secrets",), ("db",))
        self.assertTrue(main.rule_allows(rule, self.request("get", "secrets", name="db")))
        self.assertFalse(main.rule_allows(rule, self.request("get", "secrets", name="other")))
        self.assertFalse(main.rule_allows(rule, self.request("list", "secrets")))

    def test_non_resource_urls_match_exactly_or_by_trailing_star(self):
        rule = main.Rule(("get",), non_resource_urls=("/healthz", "/apis/*"))
        self.assertTrue(main.rule_allows(rule, main.Request("get", path="/healthz")))
        self.assertTrue(main.rule_allows(rule, main.Request("get", path="/apis/apps/v1")))
        self.assertFalse(main.rule_allows(rule, main.Request("get", path="/healthz/etcd")))
        self.assertFalse(main.rule_allows(rule, main.Request("post", path="/healthz")))
        self.assertFalse(main.rule_allows(main.Rule(("get",), ("",), ("*",)), main.Request("get", path="/healthz")))

    def test_star_verb_rule_does_not_answer_a_literal_question_about_other_rules(self):
        self.assertFalse(main.rule_allows(main.Rule(("get",), ("*",), ("*",)), self.request("*", "*")))


class AuthorizeTests(unittest.TestCase):
    def setUp(self):
        self.policy = fixture_policy()

    def test_fixture_is_consistent(self):
        self.assertEqual(self.policy.warnings, [])

    def test_allow_names_the_binding_like_the_api_server(self):
        decision = ask(self.policy, DASHBOARD, "get", "secrets/shop-db-readonly")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, 'RBAC: allowed by RoleBinding "pod-reader/shop" of Role "pod-reader" to ServiceAccount "dashboard/shop"')
        self.assertEqual(decision.grants[0].rule.resource_names, ("shop-db-readonly",))

    def test_permissions_are_the_union_of_every_binding(self):
        decision = ask(self.policy, DASHBOARD, "get", "pods")
        self.assertEqual([g.binding.name for g in decision.grants], ["dashboard-view", "pod-reader"])
        self.assertTrue(ask(self.policy, DASHBOARD, "watch", "pods").allowed)
        self.assertTrue(ask(self.policy, DASHBOARD, "get", "pods", subresource="log").allowed)

    def test_deny_by_default_with_the_forbidden_message(self):
        decision = ask(self.policy, DASHBOARD, "list", "secrets")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "")
        self.assertEqual(decision.forbidden, 'secrets is forbidden: User "system:serviceaccount:shop:dashboard" cannot list resource "secrets" in API group "" in the namespace "shop"')
        exec_denied = ask(self.policy, DASHBOARD, "create", "pods/web-1", subresource="exec")
        self.assertEqual(exec_denied.forbidden, 'pods "web-1" is forbidden: User "system:serviceaccount:shop:dashboard" cannot create resource "pods/exec" in API group "" in the namespace "shop"')

    def test_role_bindings_only_count_in_their_namespace(self):
        self.assertFalse(ask(self.policy, DASHBOARD, "list", "pods", namespace="default").allowed)
        cluster = ask(self.policy, DASHBOARD, "list", "pods", namespace="")
        self.assertFalse(cluster.allowed)
        self.assertIn("at the cluster scope", cluster.forbidden)

    def test_service_account_subject_without_namespace_means_the_binding_namespace(self):
        self.assertFalse(ask(self.policy, CI, "list", "pods", namespace="kube-system").allowed)
        self.assertTrue(ask(self.policy, "system:serviceaccount:kube-system:ci", "list", "pods", namespace="kube-system").allowed)

    def test_cluster_role_binding_service_account_needs_a_namespace(self):
        docs = [cluster_role("reader", [{"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}]), binding("ClusterRoleBinding", "broken", "ClusterRole", "reader", [{"kind": "ServiceAccount", "name": "ci"}])]
        self.assertFalse(ask(main.load_policy(docs), CI, "list", "pods").allowed)

    def test_user_subject_spelled_like_a_service_account_matches_it(self):
        docs = [cluster_role("reader", [{"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}]), binding("ClusterRoleBinding", "as-user", "ClusterRole", "reader", [{"kind": "User", "name": CI}])]
        self.assertTrue(ask(main.load_policy(docs), CI, "list", "pods").allowed)

    def test_group_bindings_and_aggregated_edit(self):
        self.assertTrue(ask(self.policy, "bob", "create", "pods/web-1", groups=["shop-oncall"], subresource="exec").allowed)
        self.assertFalse(ask(self.policy, "bob", "create", "pods/web-1", subresource="exec").allowed)

    def test_missing_role_is_an_error_in_the_reason(self):
        decision = ask(self.policy, "bob", "delete", "roles", groups=["shop-oncall"])
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, 'RBAC: clusterrole.rbac.authorization.k8s.io "shop-legacy-metrics" not found')
        self.assertTrue(decision.forbidden.endswith(': RBAC: clusterrole.rbac.authorization.k8s.io "shop-legacy-metrics" not found'))

    def test_two_missing_roles_are_listed_in_brackets(self):
        self.assertEqual(main.aggregate_errors(["a", "b", "a"]), "[a, b]")
        self.assertEqual(main.aggregate_errors(["a", "a"]), "a")

    def test_non_resource_urls_only_count_through_cluster_role_bindings(self):
        healthz = ask(self.policy, DASHBOARD, "get", "/healthz")
        self.assertEqual(healthz.reason, 'RBAC: allowed by ClusterRoleBinding "system:discovery" of ClusterRole "system:discovery" to Group "system:authenticated"')
        metrics = ask(self.policy, DASHBOARD, "get", "/metrics")
        self.assertEqual(metrics.forbidden, 'forbidden: User "system:serviceaccount:shop:dashboard" cannot get path "/metrics"')

    def test_system_masters_is_allowed_before_rbac(self):
        decision = ask(main.load_policy([]), "root", "delete", "nodes", namespace="", groups=["system:masters"])
        self.assertTrue(decision.allowed and decision.privileged)
        self.assertEqual(decision.reason, "")

    def test_subject_access_review_mode_loses_group_grants(self):
        self.assertTrue(ask(self.policy, DASHBOARD, "create", "selfsubjectaccessreviews", namespace="").allowed)
        self.assertFalse(ask(self.policy, DASHBOARD, "create", "selfsubjectaccessreviews", namespace="", implicit=False).allowed)

    def test_deployments_forbidden_message_qualifies_the_group(self):
        decision = ask(self.policy, DASHBOARD, "delete", "deployments/web")
        self.assertEqual(decision.forbidden, 'deployments.apps "web" is forbidden: User "system:serviceaccount:shop:dashboard" cannot delete resource "deployments" in API group "apps" in the namespace "shop"')


class AggregationTests(unittest.TestCase):
    LEASES = {"apiGroups": ["coordination.k8s.io"], "resources": ["leases"], "verbs": ["get"]}
    PODS = {"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}

    def test_aggregation_is_transitive_and_skips_the_role_itself(self):
        docs = [
            cluster_role("leases", [self.LEASES], {"agg-view": "true"}),
            cluster_role("view", [], {"agg-edit": "true", "agg-view": "true"}, [{"matchLabels": {"agg-view": "true"}}]),
            cluster_role("edit", [self.PODS], {"agg-admin": "true"}, [{"matchLabels": {"agg-edit": "true"}}]),
            cluster_role("admin", [], {}, [{"matchLabels": {"agg-admin": "true"}}]),
        ]
        policy = main.load_policy(docs)
        self.assertEqual(policy.effective["view"], [main.Rule.from_json(self.LEASES)])
        self.assertEqual(policy.effective["admin"], [main.Rule.from_json(self.LEASES)])
        self.assertTrue(any("edit" in w and "1 stale" in w for w in policy.warnings))

    def test_stale_stored_rules_warn_and_no_aggregate_keeps_them(self):
        docs = [cluster_role("leases", [self.LEASES], {"agg-view": "true"}), cluster_role("view", [], {}, [{"matchLabels": {"agg-view": "true"}}])]
        policy = main.load_policy(docs)
        self.assertIn("1 not copied yet", policy.warnings[0])
        self.assertEqual(main.load_policy(docs, aggregate=False).effective["view"], [])

    def test_match_expressions(self):
        labels = {"tier": "web", "team": "shop"}
        self.assertTrue(main.selector_matches({"matchExpressions": [{"key": "tier", "operator": "In", "values": ["web", "api"]}]}, labels))
        self.assertFalse(main.selector_matches({"matchExpressions": [{"key": "tier", "operator": "NotIn", "values": ["web"]}]}, labels))
        self.assertTrue(main.selector_matches({"matchExpressions": [{"key": "team", "operator": "Exists"}, {"key": "legacy", "operator": "DoesNotExist"}]}, labels))
        self.assertTrue(main.selector_matches({"matchLabels": {"team": "shop"}}, labels))
        self.assertTrue(main.selector_matches({}, labels))
        with self.assertRaises(main.RbacError):
            main.selector_matches({"matchExpressions": [{"key": "tier", "operator": "Gt"}]}, labels)


class ListAndWhoTests(unittest.TestCase):
    def setUp(self):
        self.policy = fixture_policy()

    def test_list_merges_verbs_per_resource_and_renders_like_kubectl(self):
        rows, errors = main.access_list(self.policy, main.identity_for(DASHBOARD), "shop")
        self.assertEqual(errors, [])
        table = {(r.resource, r.non_resource_urls, r.resource_names): r.verbs for r in rows}
        self.assertEqual(table[("pods", (), ())], ["get", "list", "watch"])
        self.assertEqual(table[("secrets", (), ("shop-db-readonly",))], ["get"])
        self.assertIn(("deployments.apps/scale", (), ()), table)
        self.assertEqual(table[("", ("/healthz",), ())], ["get"])
        self.assertEqual(rows[-1].resource, "")

    def test_breakdown_and_compact_are_inverse_for_simple_rules(self):
        rule = main.Rule(("get", "list"), ("", "apps"), ("pods",), ("a", "b"))
        pieces = main.breakdown(rule)
        self.assertEqual(len(pieces), 8)
        merged = main.compact(pieces)
        self.assertIn(main.Rule(("get", "list"), ("apps",), ("pods",), ("b",)), merged)

    def test_combine_resource_group(self):
        self.assertEqual(main.combine_resource_group("deployments/scale", "apps"), "deployments.apps/scale")
        self.assertEqual(main.combine_resource_group("pods/log", ""), "pods/log")
        self.assertEqual(main.combine_resource_group("*", "*"), "*.*")

    def test_who_can_resolves_service_accounts_and_reports_broken_bindings(self):
        request, _ = main.parse_request("list", "leases.coordination.k8s.io", "shop")
        rows, errors = main.who_can(self.policy, request)
        found = {(r.subject.kind, r.subject.label()) for r in rows}
        self.assertEqual(found, {("Group", "system:masters"), ("User", "alice"), ("ServiceAccount", "shop/dashboard"), ("Group", "shop-oncall")})
        self.assertEqual(len(errors), 1)
        self.assertIn("legacy-metrics", errors[0])
        system, _ = main.who_can(self.policy, main.parse_request("list", "pods", "kube-system")[0])
        self.assertIn(("ServiceAccount", "kube-system/ci"), {(r.subject.kind, r.subject.label()) for r in system})

    def test_group_scope_explains_wide_groups(self):
        self.assertIn("every service account in shop", main.group_scope(main.Subject("Group", "system:serviceaccounts:shop")))
        self.assertEqual(main.group_scope(main.Subject("User", "alice")), "")


class EscalationTests(unittest.TestCase):
    def setUp(self):
        self.policy = fixture_policy()
        self.ci = main.identity_for(CI)

    def test_bind_verb_allows_binding_a_role_you_do_not_hold(self):
        check = main.can_bind(self.policy, self.ci, "ClusterRole", "view", "shop")
        self.assertEqual((check.allowed, check.how), (True, "bind"))

    def test_escalation_lists_every_missing_permission(self):
        check = main.can_bind(self.policy, self.ci, "ClusterRole", "edit", "shop")
        self.assertEqual((check.allowed, check.how), (False, "escalation"))
        self.assertIn('{APIGroups:[""], Resources:["pods/exec"], Verbs:["create" "get"]}', check.message)
        self.assertTrue(check.message.startswith('user "system:serviceaccount:shop:ci" (groups=["system:serviceaccounts" "system:serviceaccounts:shop" "system:authenticated"]) is attempting to grant RBAC permissions not currently held:'))
        self.assertNotIn("pods/log\"], Verbs:[\"get\"", check.message)

    def test_resource_names_must_be_held_too(self):
        check = main.can_bind(self.policy, self.ci, "Role", "pod-reader", "shop")
        self.assertFalse(check.allowed)
        self.assertEqual([r.to_json() for r in check.missing], [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["shop-db-readonly"], "verbs": ["get"]}])
        self.assertTrue(main.can_bind(self.policy, self.ci, "Role", "binder", "shop").allowed)

    def test_create_permission_comes_first_and_masters_skip_the_check(self):
        check = main.can_bind(self.policy, main.identity_for(DASHBOARD), "ClusterRole", "view", "shop")
        self.assertEqual(check.how, "forbidden")
        self.assertIn('cannot create resource "rolebindings"', check.message)
        cluster = main.can_bind(self.policy, main.identity_for("alice"), "ClusterRole", "edit", "")
        self.assertIn("at the cluster scope", cluster.message)
        root = main.can_bind(self.policy, main.identity_for("root", ["system:masters"]), "ClusterRole", "cluster-admin", "")
        self.assertEqual((root.allowed, root.how), (True, "privileged"))
        missing = main.can_bind(self.policy, self.ci, "ClusterRole", "nope", "shop")
        self.assertEqual(missing.how, "not-found")
        with self.assertRaises(main.RbacError):
            main.can_bind(self.policy, self.ci, "Role", "binder", "")

    def test_rule_covers_follows_the_api_server(self):
        owner = main.Rule(("*",), ("apps",), ("*/scale", "deployments"))
        self.assertTrue(main.rule_covers(owner, main.Rule(("update",), ("apps",), ("statefulsets/scale",))))
        self.assertFalse(main.rule_covers(owner, main.Rule(("update",), ("apps",), ("statefulsets",))))
        named = main.Rule(("get",), ("",), ("secrets",), ("db",))
        self.assertTrue(main.rule_covers(main.Rule(("get",), ("",), ("secrets",)), named))
        self.assertFalse(main.rule_covers(named, main.Rule(("get",), ("",), ("secrets",))))
        self.assertTrue(main.rule_covers(main.Rule(("get",), non_resource_urls=("/apis/*",)), main.Rule(("get",), non_resource_urls=("/apis/apps",))))
        self.assertFalse(main.rule_covers(main.Rule(("get",), ("*",), ("*",)), main.Rule(("get",), non_resource_urls=("/healthz",))))

    def test_compact_string_matches_go_formatting(self):
        rule = main.Rule(("create", "delete"), ("apps",), ("deployments",), ("web",))
        self.assertEqual(main.compact_string(rule), '{APIGroups:["apps"], Resources:["deployments"], ResourceNames:["web"], Verbs:["create" "delete"]}')
        self.assertEqual(main.compact_string(main.Rule(("get",), non_resource_urls=("/healthz",))), '{NonResourceURLs:["/healthz"], Verbs:["get"]}')


class LoadTests(unittest.TestCase):
    def test_later_files_override_earlier_objects_for_what_if_reviews(self):
        base = [cluster_role("reader", []), binding("RoleBinding", "r", "ClusterRole", "reader", [{"kind": "User", "name": "jane"}], "shop")]
        proposed = {"kind": "ClusterRole", "metadata": {"name": "reader"}, "rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}]}
        self.assertFalse(ask(main.load_policy([base]), "jane", "list", "pods").allowed)
        self.assertTrue(ask(main.load_policy([base, proposed]), "jane", "list", "pods").allowed)

    def test_typed_lists_and_single_objects_load_and_other_kinds_are_ignored(self):
        docs = [{"kind": "RoleList", "items": [{"metadata": {"name": "r", "namespace": "shop"}, "rules": []}]}, {"kind": "ServiceAccount", "metadata": {"name": "x"}}]
        policy = main.load_policy(docs)
        self.assertIn(("shop", "r"), policy.roles)

    def test_bad_input_is_explained(self):
        with self.assertRaises(main.RbacError):
            main.load_policy([{"kind": "RoleBinding", "metadata": {"name": "b"}, "roleRef": {"kind": "Pod", "name": "x"}}])
        with self.assertRaises(main.RbacError):
            main.load_policy([binding("RoleBinding", "b", "Role", "r", [{"kind": "Robot", "name": "x"}])])
        with self.assertRaises(main.RbacError):
            main.load_policy(["not an object"])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rbac.yaml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("kind: List\n")
            with self.assertRaises(main.RbacError) as caught:
                main.read_documents([path])
            self.assertIn("-o json", str(caught.exception))
        self.assertEqual(main.read_documents(["-"], io.StringIO('{"kind": "List", "items": []}')), [{"kind": "List", "items": []}])


class CliTests(unittest.TestCase):
    def test_can_i_exit_codes_and_json(self):
        code, out, _ = run_cli("can-i", "get", "pods", "--subresource", "log", "-n", "shop", "--as", DASHBOARD, "-f", FIXTURE, "--json")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertTrue(doc["allowed"])
        self.assertEqual(doc["identity"]["groups"][-1], "system:authenticated")
        self.assertEqual(doc["grants"][0]["role"], {"kind": "ClusterRole", "name": "view"})
        code, out, _ = run_cli("can-i", "list", "secrets", "-n", "shop", "--as", DASHBOARD, "-f", FIXTURE)
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("no\n"))
        self.assertIn("deny-by-default", out)

    def test_can_i_list_prints_the_kubectl_table(self):
        code, out, _ = run_cli("can-i", "--list", "-n", "shop", "--as", DASHBOARD, "-f", FIXTURE)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Resources"))
        self.assertIn("[shop-db-readonly]", out)
        code, out, _ = run_cli("can-i", "--list", "-n", "shop", "--as", "bob", "--as-group", "shop-oncall", "-f", FIXTURE, "--json")
        self.assertEqual(len(json.loads(out)["errors"]), 1)

    def test_who_can_and_can_bind(self):
        code, out, _ = run_cli("who-can", "create", "pods", "--subresource", "exec", "-n", "shop", "-f", FIXTURE)
        self.assertEqual(code, 0)
        self.assertIn("shop-oncall", out)
        self.assertNotIn("dashboard", out)
        code, out, _ = run_cli("can-bind", "--clusterrole", "edit", "-n", "shop", "--as", CI, "-f", FIXTURE, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["how"], "escalation")
        code, _, _ = run_cli("can-bind", "--clusterrole", "view", "-n", "shop", "--as", CI, "-f", FIXTURE)
        self.assertEqual(code, 0)

    def test_errors_exit_two_with_a_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = run_cli("can-i", "get", "pods", "--as", "jane", "-f", os.path.join(tmp, "rbac.json"))
        self.assertEqual(code, 2)
        self.assertIn("kubectl get roles,rolebindings,clusterroles,clusterrolebindings -A -o json", err)
        code, _, err = run_cli("can-i", "get", "pods", "-f", FIXTURE)
        self.assertEqual(code, 2)
        self.assertIn("--as", err)
        code, _, err = run_cli("can-i", "get", "pods", "--as-group", "ops", "-f", FIXTURE)
        self.assertIn("--as-group needs --as", err)

    def test_whoami_needs_no_dump(self):
        code, out, _ = run_cli("whoami", "--as", DASHBOARD, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"username": DASHBOARD, "groups": ["system:serviceaccounts", "system:serviceaccounts:shop", "system:authenticated"]})


if __name__ == "__main__":
    unittest.main()
