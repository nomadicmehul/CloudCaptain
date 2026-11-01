import copy
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "routes.json")


def fixture_items():
    with open(FIXTURE, encoding="utf-8") as handle:
        return main.flatten(json.load(handle))


def gateway(listeners, namespace="shop", name="web"):
    return {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "Gateway", "metadata": {"name": name, "namespace": namespace}, "spec": {"gatewayClassName": "eg", "listeners": listeners}}


def listener(name="http", port=80, hostname=None, source="Same", labels=None, protocol="HTTP"):
    raw = {"name": name, "protocol": protocol, "port": port, "allowedRoutes": {"namespaces": {"from": source}}}
    if hostname:
        raw["hostname"] = hostname
    if labels:
        raw["allowedRoutes"]["namespaces"]["selector"] = {"matchLabels": labels}
    return raw


def route(name, rules, namespace="shop", hostnames=None, parents=None, created=""):
    spec = {"parentRefs": parents if parents is not None else [{"name": "web", "namespace": "shop"}], "rules": rules}
    if hostnames:
        spec["hostnames"] = hostnames
    return {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute", "metadata": {"name": name, "namespace": namespace, "creationTimestamp": created}, "spec": spec}


def service(name, namespace="shop"):
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": namespace}, "spec": {"ports": [{"port": 8080}]}}


def rule(path="/", kind="PathPrefix", backend="blue", headers=None, query=None, method=None, filters=None, backends=None):
    match = {"path": {"type": kind, "value": path}}
    if headers:
        match["headers"] = [{"name": k, "value": v} for k, v in headers.items()]
    if query:
        match["queryParams"] = [{"name": k, "value": v} for k, v in query.items()]
    if method:
        match["method"] = method
    raw = {"matches": [match]}
    if filters:
        raw["filters"] = filters
    if backends is not None:
        raw["backendRefs"] = backends
    elif backend:
        raw["backendRefs"] = [{"name": backend, "port": 8080}]
    return raw


def router_for(items, **kwargs):
    return main.Router(main.build_world(items), **kwargs)


def ask(router, path, host="app.shop.test", method="GET", headers=None, port=80):
    request = main.make_request(method, host, path, headers or {}, port)
    return router.decide(request), request


def conditions(report):
    found = {}
    for entry in report["routes"]:
        for parent in entry["status"]["parents"]:
            for c in parent["conditions"]:
                found[(entry["name"], c["type"])] = (c["status"], c["reason"])
    return found


class LoadingTests(unittest.TestCase):
    def test_flatten_accepts_a_list_an_array_and_single_objects(self):
        one = service("a")
        self.assertEqual(main.flatten(one), [one])
        self.assertEqual(main.flatten([one, [one]]), [one, one])
        self.assertEqual(main.flatten({"kind": "List", "items": [one]}), [one])
        self.assertEqual(main.flatten({"no": "kind"}), [])
        world = main.build_world(fixture_items())
        self.assertEqual([g.key for g in world.gateways], ["shop/web"])
        self.assertEqual([r.name for r in world.routes], ["app", "broken", "elsewhere"])
        self.assertEqual(sorted(world.services), ["shop/blue", "shop/green"])
        app = world.routes[0]
        self.assertEqual(app.rules[4].backends[0].weight, 90)
        self.assertEqual(app.parents[0].namespace, "shop")
        self.assertEqual(world.routes[2].rules[0].matches[0].path_type, "PathPrefix")

    def test_validation_mirrors_the_crd_rules(self):
        cases = [
            (rule("api", "Exact"), 'must be an absolute path starting with "/"'),
            (rule("/a", "Glob"), "is not Exact, PathPrefix or RegularExpression"),
            (rule("/old", "Exact", filters=[{"type": "RequestRedirect", "requestRedirect": {"statusCode": 301}}]), "must not be used together with backendRefs"),
            (rule("/a", backend=None, backends=[], filters=[{"type": "RequestRedirect", "requestRedirect": {}}, {"type": "URLRewrite", "urlRewrite": {}}]), "either RequestRedirect or URLRewrite"),
            (rule("/a", "Exact", filters=[{"type": "URLRewrite", "urlRewrite": {"path": {"type": "ReplacePrefixMatch", "replacePrefixMatch": "/b"}}}]), "exactly one PathPrefix match"),
            (rule("/a", backends=[{"name": "blue"}]), "port is required"),
            (rule("/a", backends=[{"name": "blue", "port": 8080, "weight": -1}]), "weight: must be an integer"),
        ]
        for raw, message in cases:
            with self.assertRaises(main.InputError) as ctx:
                main.build_world([route("bad", [raw])])
            self.assertIn(message, str(ctx.exception))
            self.assertIn("HTTPRoute shop/bad: spec.rules[0]", str(ctx.exception))
        world = main.build_world([route("odd", [rule("/a", filters=[{"type": "ExtensionRef", "extensionRef": {}}])])])
        self.assertIn("ExtensionRef is not implemented", world.warnings[0])

    def test_read_documents_reports_the_file_and_line_of_bad_json(self):
        folder = tempfile.mkdtemp(prefix="minigateway-")
        self.addCleanup(shutil.rmtree, folder)
        shutil.copy(FIXTURE, os.path.join(folder, "a.json"))
        self.assertEqual(len(main.read_documents([folder])), 6)
        bad = os.path.join(folder, "b.json")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write('{\n  "kind": "Service",\n  oops\n}')
        with self.assertRaises(main.InputError) as ctx:
            main.read_documents([bad])
        self.assertIn("b.json:3: invalid JSON", str(ctx.exception))


class MatchingTests(unittest.TestCase):
    def test_path_prefix_compares_whole_segments(self):
        self.assertTrue(main.path_matches("PathPrefix", "/api", "/api"))
        self.assertTrue(main.path_matches("PathPrefix", "/api", "/api/"))
        self.assertTrue(main.path_matches("PathPrefix", "/api", "/api/v1/users"))
        self.assertTrue(main.path_matches("PathPrefix", "/api/", "/api"))
        self.assertFalse(main.path_matches("PathPrefix", "/api", "/apis"))
        self.assertFalse(main.path_matches("PathPrefix", "/api", "/ap"))
        self.assertTrue(main.path_matches("PathPrefix", "/", "/anything"))
        self.assertTrue(main.path_matches("Exact", "/api", "/api"))
        self.assertFalse(main.path_matches("Exact", "/api", "/api/"))
        self.assertTrue(main.path_matches("RegularExpression", r"/user/\d+", "/user/42"))

    def test_headers_ignore_name_case_query_names_do_not_and_conditions_are_anded(self):
        world = main.build_world([route("r", [rule("/a", headers={"X-Version": "v2"}, query={"Debug": "1"}, method="post")])])
        match = world.routes[0].rules[0].matches[0]
        self.assertEqual((match.headers[0].name, match.method), ("x-version", "POST"))
        ok = main.make_request("POST", "h", "/a?Debug=1", {"x-VERSION": "v2"}, 80)
        self.assertTrue(main.request_matches(match, ok))
        self.assertFalse(main.request_matches(match, main.make_request("POST", "h", "/a?debug=1", {"x-version": "v2"}, 80)))
        self.assertFalse(main.request_matches(match, main.make_request("GET", "h", "/a?Debug=1", {"x-version": "v2"}, 80)))
        self.assertFalse(main.request_matches(match, main.make_request("POST", "h", "/a?Debug=1", {"x-version": "V2"}, 80)))

    def test_matches_inside_one_rule_are_ored(self):
        raw = {"matches": [{"path": {"type": "Exact", "value": "/a"}}, {"path": {"type": "Exact", "value": "/b"}}], "backendRefs": [{"name": "blue", "port": 8080}]}
        router = router_for([gateway([listener()]), service("blue"), route("r", [raw])])
        self.assertEqual(ask(router, "/a")[0].winner.match_index, 0)
        self.assertEqual(ask(router, "/b")[0].winner.match_index, 1)
        self.assertEqual(ask(router, "/c")[0].status, 404)


class HostnameTests(unittest.TestCase):
    def test_wildcards_match_one_or_more_labels_but_not_the_apex(self):
        self.assertTrue(main.hostname_matches("*.shop.test", "app.shop.test"))
        self.assertTrue(main.hostname_matches("*.shop.test", "a.b.shop.test"))
        self.assertFalse(main.hostname_matches("*.shop.test", "shop.test"))
        self.assertFalse(main.hostname_matches("*.shop.test", "app.shop.testing"))
        self.assertTrue(main.hostname_matches("app.shop.test", "app.shop.test"))

    def test_listener_and_route_hostnames_intersect(self):
        self.assertEqual(main.intersect(None, "app.shop.test"), "app.shop.test")
        self.assertEqual(main.intersect("*.shop.test", "app.shop.test"), "app.shop.test")
        self.assertEqual(main.intersect("app.shop.test", "*.shop.test"), "app.shop.test")
        self.assertEqual(main.intersect("*.shop.test", "*.eu.shop.test"), "*.eu.shop.test")
        self.assertEqual(main.intersect("*.eu.shop.test", "*.shop.test"), "*.eu.shop.test")
        self.assertIsNone(main.intersect("*.shop.test", "shop.test"))
        self.assertIsNone(main.intersect("app.shop.test", "api.shop.test"))
        self.assertIsNone(main.intersect("*.shop.test", "*.other.test"))
        world = main.build_world([gateway([listener(hostname="*.shop.test")]), route("r", [rule()], hostnames=["app.shop.test", "app.other.test"])])
        self.assertEqual(main.effective_hostnames(world.gateways[0].listeners[0], world.routes[0]), ["app.shop.test"])

    def test_the_more_specific_hostname_wins_before_any_path_rule(self):
        items = [gateway([listener()]), service("blue"), service("green"), route("wild", [rule("/api/health", "Exact", backend="blue")], hostnames=["*.shop.test"]), route("exact", [rule("/", backend="green")], hostnames=["app.shop.test"])]
        decision, _ = ask(router_for(items), "/api/health")
        self.assertEqual((decision.winner.attachment.route.name, decision.why), ("exact", "exact hostname"))
        decision, _ = ask(router_for(items), "/api/health", host="api.shop.test")
        self.assertEqual(decision.winner.attachment.route.name, "wild")


class AttachmentTests(unittest.TestCase):
    def test_fixture_status_has_one_accepted_one_unresolved_and_one_rejected_route(self):
        report = main.status_json(main.build_world(fixture_items()))
        found = conditions(report)
        self.assertEqual(found[("app", "Accepted")], ("True", "Accepted"))
        self.assertEqual(found[("app", "ResolvedRefs")], ("True", "ResolvedRefs"))
        self.assertEqual(found[("broken", "Accepted")], ("True", "Accepted"))
        self.assertEqual(found[("broken", "ResolvedRefs")], ("False", "BackendNotFound"))
        self.assertEqual(found[("elsewhere", "Accepted")], ("False", "NoMatchingListenerHostname"))
        parent = report["routes"][0]["status"]["parents"][0]
        self.assertEqual(parent["parentRef"], {"group": "gateway.networking.k8s.io", "kind": "Gateway", "namespace": "shop", "name": "web"})
        self.assertEqual(parent["controllerName"], main.CONTROLLER)
        self.assertEqual(report["gateways"][0]["listeners"][0]["acceptedRoutes"], 2)
        text = main.render_status(report)
        self.assertIn("ResolvedRefs=False (BackendNotFound): service shop/missing not found", text)
        self.assertIn("listener http :80 HTTP *.shop.test: 2 route(s) accepted", text)

    def test_allowed_routes_same_all_and_selector(self):
        foreign = route("r", [rule()], namespace="team-a", parents=[{"name": "web", "namespace": "shop"}])
        team = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "team-a", "labels": {"gateway-access": "yes"}}}
        for listeners, extra, expected in [
            ([listener(source="Same")], [], ("False", "NotAllowedByListeners")),
            ([listener(source="All")], [], ("True", "Accepted")),
            ([listener(source="Selector", labels={"gateway-access": "yes"})], [team], ("True", "Accepted")),
            ([listener(source="Selector", labels={"gateway-access": "yes"})], [], ("False", "NotAllowedByListeners")),
            ([listener(source="Selector", labels={"kubernetes.io/metadata.name": "team-a"})], [], ("True", "Accepted")),
            ([listener(source="All", protocol="TCP")], [], ("False", "NotAllowedByListeners")),
        ]:
            report = main.status_json(main.build_world([gateway(listeners), foreign] + extra))
            self.assertEqual(conditions(report)[("r", "Accepted")], expected, listeners)

    def test_section_name_and_port_must_name_a_listener(self):
        listeners = [listener("http", 80), listener("admin", 8080)]
        for parent, expected in [
            ({"name": "web", "sectionName": "admin"}, ("True", "Accepted")),
            ({"name": "web", "sectionName": "nope"}, ("False", "NoMatchingParent")),
            ({"name": "web", "port": 8080}, ("True", "Accepted")),
            ({"name": "web", "sectionName": "http", "port": 8080}, ("False", "NoMatchingParent")),
        ]:
            world = main.build_world([gateway(listeners), route("r", [rule()], parents=[parent])])
            self.assertEqual(conditions(main.status_json(world))[("r", "Accepted")], expected, parent)
        router = router_for([gateway(listeners), service("blue"), route("r", [rule()], parents=[{"name": "web", "sectionName": "admin"}])])
        self.assertEqual(ask(router, "/", port=80)[0].status, 404)
        self.assertEqual(ask(router, "/", port=8080)[0].action, "forward")

    def test_a_parent_that_does_not_exist_gets_no_status_only_a_warning(self):
        report = main.status_json(main.build_world([gateway([listener()]), route("r", [rule()], parents=[{"name": "wbe"}])]))
        self.assertEqual(report["routes"][0]["status"]["parents"], [])
        self.assertIn("parentRef Gateway shop/wbe not found; no controller writes status for it", report["problems"][0])
        self.assertIn("(no status: no parentRef names a Gateway in the input)", main.render_status(report))

    def test_cross_namespace_backends_need_a_reference_grant_in_the_target_namespace(self):
        backend = [{"name": "api", "namespace": "payments", "port": 8080}]
        base = [gateway([listener()]), service("api", "payments"), route("r", [rule(backends=backend)])]
        grant = {"apiVersion": "gateway.networking.k8s.io/v1beta1", "kind": "ReferenceGrant", "metadata": {"name": "g", "namespace": "payments"}, "spec": {"from": [{"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "namespace": "shop"}], "to": [{"group": "", "kind": "Service"}]}}
        self.assertEqual(conditions(main.status_json(main.build_world(base)))[("r", "ResolvedRefs")], ("False", "RefNotPermitted"))
        self.assertEqual(conditions(main.status_json(main.build_world(base + [grant])))[("r", "ResolvedRefs")], ("True", "ResolvedRefs"))
        wrong_place = copy.deepcopy(grant)
        wrong_place["metadata"]["namespace"] = "shop"
        self.assertEqual(conditions(main.status_json(main.build_world(base + [wrong_place])))[("r", "ResolvedRefs")], ("False", "RefNotPermitted"))
        named = copy.deepcopy(grant)
        named["spec"]["to"][0]["name"] = "other"
        self.assertEqual(conditions(main.status_json(main.build_world(base + [named])))[("r", "ResolvedRefs")], ("False", "RefNotPermitted"))
        odd_kind = [gateway([listener()]), route("r", [rule(backends=[{"name": "b", "kind": "Bucket", "group": "example.com"}])])]
        self.assertEqual(conditions(main.status_json(main.build_world(odd_kind)))[("r", "ResolvedRefs")], ("False", "InvalidKind"))


class PrecedenceTests(unittest.TestCase):
    def test_fixture_exact_beats_prefix_and_headers_and_segments_are_respected(self):
        router = router_for(fixture_items())
        decision, _ = ask(router, "/api/health", headers={"x-version": "v2"})
        self.assertEqual((decision.winner.rule.index, decision.why), (0, "exact path"))
        self.assertEqual(decision.headers["x-route"], "exact")
        self.assertEqual([s.backend.name for s in decision.splits], ["green"])
        decision, _ = ask(router, "/api/users", headers={"x-version": "v2"})
        self.assertEqual((decision.winner.rule.index, decision.why), (2, "more header matches"))
        self.assertEqual(ask(router, "/api/users")[0].winner.rule.index, 1)
        self.assertEqual(ask(router, "/apis")[0].status, 404)
        self.assertEqual(ask(router, "/", host="other.example")[0].status, 404)

    def test_the_spec_order_longest_prefix_method_headers_query_age_name(self):
        rules = [
            rule("/shop", backend="a"),
            rule("/shop/cart", backend="b"),
            rule("/shop/cart", method="POST", backend="c"),
            rule("/shop/cart", headers={"x-a": "1"}, backend="d"),
            rule("/shop/cart", query={"q": "1"}, backend="e"),
        ]
        items = [gateway([listener()])] + [service(n) for n in "abcde"] + [route("r", rules)]
        router = router_for(items)

        def winner(path, method="GET", headers=None):
            decision, _ = ask(router, path, method=method, headers=headers)
            return decision.splits[0].backend.name, decision.why

        self.assertEqual(winner("/shop/shoes"), ("a", "only match"))
        self.assertEqual(winner("/shop/cart/items"), ("b", "longer path"))
        self.assertEqual(winner("/shop/cart", method="POST", headers={"x-a": "1"}), ("c", "method match"))
        self.assertEqual(winner("/shop/cart?q=1", headers={"x-a": "1"}), ("d", "more header matches"))
        self.assertEqual(winner("/shop/cart?q=1"), ("e", "more query param matches"))

    def test_ties_across_routes_go_to_the_oldest_then_namespace_name_then_rule_order(self):
        items = [gateway([listener(source="All")]), service("old", "a"), service("new", "a"), service("zeta", "z")]
        items += [route("new", [rule("/x", backend="new")], namespace="a", created="2026-10-02T00:00:00Z")]
        items += [route("old", [rule("/x", backend="old")], namespace="a", created="2026-10-01T00:00:00Z")]
        decision, _ = ask(router_for(items), "/x")
        self.assertEqual((decision.winner.attachment.route.name, decision.why), ("old", "older route"))
        items = [gateway([listener(source="All")]), service("zeta", "z"), service("alpha", "a")]
        items += [route("r", [rule("/x", backend="zeta")], namespace="z", created="2026-10-01T00:00:00Z")]
        items += [route("r", [rule("/x", backend="alpha")], namespace="a", created="2026-10-01T00:00:00Z")]
        decision, _ = ask(router_for(items), "/x")
        self.assertEqual((decision.winner.attachment.route.key, decision.why), ("a/r", "route namespace/name"))
        items = [gateway([listener()]), service("one"), service("two"), route("r", [rule("/x", backend="one"), rule("/x", backend="two")])]
        decision, _ = ask(router_for(items), "/x")
        self.assertEqual((decision.winner.rule.index, decision.why), (0, "rule order"))

    def test_table_lists_every_attached_match_in_precedence_order(self):
        rows = [main.describe(c) for c in router_for(fixture_items()).table(80)]
        self.assertEqual(rows[0], "shop/app rule 0 match 0: Exact /api/health")
        self.assertLess(rows.index("shop/app rule 2 match 0: PathPrefix /api, headers x-version=v2"), rows.index("shop/app rule 1 match 0: PathPrefix /api"))
        self.assertFalse(any("elsewhere" in row for row in rows))


class FilterTests(unittest.TestCase):
    def test_request_header_modifier_sets_adds_and_removes(self):
        spec = {"set": [{"name": "X-Env", "value": "prod"}], "add": [{"name": "x-tag", "value": "b"}, {"name": "x-new", "value": "1"}], "remove": ["X-Debug"]}
        result = main.modify_headers({"x-env": "dev", "x-tag": "a", "x-debug": "on", "accept": "*/*"}, spec)
        self.assertEqual(result, {"x-env": "prod", "x-tag": "a,b", "x-new": "1", "accept": "*/*"})

    def test_replace_prefix_match_follows_the_spec_table(self):
        table = [
            ("/foo/bar", "/foo", "/xyz", "/xyz/bar"),
            ("/foo/bar", "/foo", "/xyz/", "/xyz/bar"),
            ("/foo/bar", "/foo/", "/xyz", "/xyz/bar"),
            ("/foo/bar", "/foo/", "/xyz/", "/xyz/bar"),
            ("/foo", "/foo", "/xyz", "/xyz"),
            ("/foo/", "/foo", "/xyz", "/xyz/"),
            ("/foo/bar", "/foo", "", "/bar"),
            ("/foo/", "/foo", "", "/"),
            ("/foo", "/foo", "", "/"),
            ("/foo/", "/foo", "/", "/"),
            ("/foo", "/foo", "/", "/"),
        ]
        for path, prefix, replacement, expected in table:
            self.assertEqual(main.replace_prefix(path, prefix, replacement), expected, (path, prefix, replacement))

    def test_redirect_location_scheme_host_port_and_status(self):
        def location(spec, path="/old?x=1"):
            items = [gateway([listener(port=8080)]), route("r", [rule("/old", backend=None, backends=[], filters=[{"type": "RequestRedirect", "requestRedirect": spec}])])]
            decision, _ = ask(router_for(items), path, port=8080)
            return decision.status, decision.location

        self.assertEqual(location({}), (302, "http://app.shop.test:8080/old?x=1"))
        self.assertEqual(location({"scheme": "https", "statusCode": 301}), (301, "https://app.shop.test/old?x=1"))
        self.assertEqual(location({"scheme": "https", "port": 8443}), (302, "https://app.shop.test:8443/old?x=1"))
        self.assertEqual(location({"hostname": "new.shop.test", "port": 80, "path": {"type": "ReplaceFullPath", "replaceFullPath": "/new"}}), (302, "http://new.shop.test/new?x=1"))
        self.assertEqual(location({"path": {"type": "ReplacePrefixMatch", "replacePrefixMatch": "/v2"}}, "/old/a"), (302, "http://app.shop.test:8080/v2/a"))
        decision, _ = ask(router_for(fixture_items()), "/old")
        self.assertEqual((decision.status, decision.location), (301, "http://app.shop.test/api/v1"))

    def test_url_rewrite_changes_the_upstream_path_and_host(self):
        decision, request = ask(router_for(fixture_items()), "/v1/users?page=2")
        self.assertEqual((decision.path, decision.query, decision.host), ("/api/users", "page=2", "app.shop.test"))
        items = [gateway([listener()]), service("blue"), route("r", [rule("/a", filters=[{"type": "URLRewrite", "urlRewrite": {"hostname": "internal.svc", "path": {"type": "ReplaceFullPath", "replaceFullPath": "/"}}}])])]
        decision, _ = ask(router_for(items), "/a/b")
        self.assertEqual((decision.path, decision.host), ("/", "internal.svc"))
        self.assertIn("upstream request: Host internal.svc, path /", main.render_decision(decision, request, "shop/web"))


class BackendTests(unittest.TestCase):
    def test_weights_split_traffic_and_zero_never_gets_any(self):
        backends = [main.BackendRef("blue", "shop", 8080, 90), main.BackendRef("green", "shop", 8080, 10), main.BackendRef("off", "shop", 8080, 0)]
        rng = random.Random(1)
        picks = [main.pick_backend(backends, rng).name for _ in range(2000)]
        self.assertNotIn("off", picks)
        self.assertAlmostEqual(picks.count("green") / 2000, 0.10, delta=0.03)
        self.assertIsNone(main.pick_backend([main.BackendRef("off", "shop", 8080, 0)], rng))
        splits = main.weight_splits(router_for(fixture_items()).table(80)[2], main.build_world(fixture_items()))
        self.assertEqual([(s.backend.name, s.percent) for s in splits], [("blue", 90.0), ("green", 10.0)])

    def test_invalid_backends_get_500_for_their_share_and_empty_rules_get_500(self):
        router = router_for(fixture_items())
        rng = random.Random(3)
        decision = router.decide(main.make_request("GET", "app.shop.test", "/broken", {}, 80), rng)
        self.assertEqual((decision.status, decision.reason), (500, "BackendNotFound: service shop/missing not found"))
        half = [gateway([listener()]), service("blue"), route("r", [rule(backends=[{"name": "blue", "port": 8080}, {"name": "gone", "port": 8080}])])]
        router = router_for(half)
        statuses = [router.decide(main.make_request("GET", "h", "/", {}, 80), rng).status for _ in range(400)]
        self.assertAlmostEqual(statuses.count(500) / 400, 0.5, delta=0.1)
        self.assertEqual(set(statuses), {None, 500})
        empty = router_for([gateway([listener()]), route("r", [rule(backends=[])])])
        self.assertEqual(ask(empty, "/")[0].status, 500)


class CliTests(unittest.TestCase):
    def run_main(self, argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_route_and_status_commands_print_json_and_text(self):
        code, out, _ = self.run_main(["route", FIXTURE, "--host", "app.shop.test", "--path", "/api/health", "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["winner"], {"route": "shop/app", "rule": 0, "match": 0, "listener": "http"})
        self.assertEqual(report["upstream"]["headers"]["x-route"], "exact")
        self.assertEqual(report["backends"][0]["name"], "green")
        code, out, _ = self.run_main(["route", FIXTURE, "--host", "app.shop.test", "--path", "/canary"])
        self.assertIn("forward: shop/green:8080 weight 10 (10.0%)", out)
        code, out, _ = self.run_main(["status", FIXTURE, "--json"])
        self.assertEqual(conditions(json.loads(out))[("elsewhere", "Accepted")], ("False", "NoMatchingListenerHostname"))
        code, out, _ = self.run_main(["table", FIXTURE])
        self.assertIn("shop/web port 80: 7 match(es) in precedence order", out)

    def test_errors_exit_two_with_the_reason(self):
        code, _, err = self.run_main(["route", os.path.join(tempfile.gettempdir(), "minigateway-missing.json"), "--host", "h"])
        self.assertEqual(code, 2)
        self.assertIn("No such file", err)
        code, _, err = self.run_main(["route", FIXTURE, "--host", "h", "-H", "no-colon"])
        self.assertEqual(code, 2)
        self.assertIn("expected 'Name: value'", err)
        two = tempfile.mkdtemp(prefix="minigateway-")
        self.addCleanup(shutil.rmtree, two)
        with open(os.path.join(two, "g.json"), "w", encoding="utf-8") as handle:
            json.dump([gateway([listener()], name="a"), gateway([listener()], name="b")], handle)
        code, _, err = self.run_main(["route", two, "--host", "h"])
        self.assertEqual(code, 2)
        self.assertIn("pass --gateway NAMESPACE/NAME", err)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.servers = []
        self.urls = {}
        for name in ("blue", "green"):
            echo = main.EchoServer(("127.0.0.1", 0), name)
            self.servers.append(echo)
            self.urls[f"shop/{name}"] = f"http://127.0.0.1:{echo.server_address[1]}"
        world = main.build_world(fixture_items())
        router = main.Router(world)
        gateway_server = main.GatewayServer(("127.0.0.1", 0), router, 80, self.urls, random.Random(7))
        self.servers.append(gateway_server)
        self.base = f"http://127.0.0.1:{gateway_server.server_address[1]}"
        main.start_servers(self.servers)
        for server in self.servers:
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def get(self, path, host="app.shop.test", headers=None):
        request = urllib.request.Request(self.base + path, headers=dict({"Host": host}, **(headers or {})))
        try:
            with main.OPENER.open(request, timeout=5) as response:
                return response.status, dict(response.getheaders()), response.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers.items()), err.read().decode()

    def test_the_proxy_routes_rewrites_redirects_and_splits_real_requests(self):
        status, headers, body = self.get("/api/health")
        seen = json.loads(body)
        self.assertEqual((status, seen["backend"], seen["headers"]["x-route"]), (200, "green", "exact"))
        self.assertEqual(headers["x-minigateway-route"], "shop/app rule 0")
        self.assertEqual(json.loads(self.get("/api/users")[2])["backend"], "blue")
        self.assertEqual(json.loads(self.get("/api/users", headers={"x-version": "v2"})[2])["backend"], "green")
        rewritten = json.loads(self.get("/v1/users?page=2")[2])
        self.assertEqual((rewritten["path"], rewritten["query"], rewritten["host"]), ("/api/users", "page=2", "app.shop.test"))
        status, headers, _ = self.get("/old")
        self.assertEqual((status, headers["Location"]), (301, "http://app.shop.test/api/v1"))
        self.assertEqual(self.get("/apis")[0], 404)
        self.assertEqual(self.get("/broken")[0], 500)
        self.assertEqual(self.get("/", host="other.example")[0], 404)
        names = [json.loads(self.get("/canary")[2])["backend"] for _ in range(200)]
        self.assertEqual(len(names), 200)
        self.assertTrue(8 <= names.count("green") <= 34, names.count("green"))

    def test_a_service_without_an_address_answers_503(self):
        del self.urls["shop/blue"]
        self.assertEqual(self.get("/api/users")[0], 503)


if __name__ == "__main__":
    unittest.main()
